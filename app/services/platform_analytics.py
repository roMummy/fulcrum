from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session, selectinload

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Message, MessageActivityHourly
from app.models.saas import (
    BillingOrder,
    BotBinding,
    ProviderNode,
    Tenant,
    TenantSession,
    TenantSubscription,
)
from app.services.channel_labels import get_channel_type_label


# 平台大盘只允许三个固定范围，既限制跨租户查询成本，也让页面和服务共享单一统计口径。
ANALYTICS_RANGES: dict[str, dict[str, object]] = {
    "24h": {"label": "24 小时", "delta": timedelta(hours=24), "granularity": "hour", "bucket_count": 24},
    "7d": {"label": "7 天", "delta": timedelta(days=7), "granularity": "day", "bucket_count": 7},
    "30d": {"label": "30 天", "delta": timedelta(days=30), "granularity": "day", "bucket_count": 30},
}


def normalize_analytics_range(value: str | None) -> str:
    """把页面查询参数收敛到固定范围，非法值直接回到默认七天。"""

    normalized = str(value or "").strip().lower()
    return normalized if normalized in ANALYTICS_RANGES else "7d"


def _build_range_context(range_key: str, current: datetime) -> dict[str, object]:
    """生成完整时间桶；空数据时也返回稳定轴线，避免图表布局随数据量变化。"""

    config = ANALYTICS_RANGES[range_key]
    granularity = str(config["granularity"])
    bucket_count = int(config["bucket_count"])
    if granularity == "hour":
        end_bucket = current.replace(minute=0, second=0, microsecond=0)
        start_bucket = end_bucket - timedelta(hours=bucket_count - 1)
        buckets = [start_bucket + timedelta(hours=index) for index in range(bucket_count)]
    else:
        end_bucket = current.replace(hour=0, minute=0, second=0, microsecond=0)
        start_bucket = end_bucket - timedelta(days=bucket_count - 1)
        buckets = [start_bucket + timedelta(days=index) for index in range(bucket_count)]
    return {
        "key": range_key,
        "label": str(config["label"]),
        "granularity": granularity,
        "start": start_bucket,
        "end": current,
        "buckets": buckets,
    }


def _bucket_datetime(value: datetime, granularity: str) -> datetime:
    """把数据库时间统一归入上海本地小时或自然日，不保留第二套 UTC 展示口径。"""

    normalized = ensure_shanghai_datetime(value)
    if granularity == "hour":
        return normalized.replace(minute=0, second=0, microsecond=0)
    return normalized.replace(hour=0, minute=0, second=0, microsecond=0)


def _bucket_label(value: datetime, granularity: str) -> str:
    """生成紧凑时间标签，桌面和移动端复用同一组服务端数据。"""

    return value.strftime("%H:%M") if granularity == "hour" else value.strftime("%m/%d")


def _time_group_expressions(column, granularity: str) -> tuple[Any, ...]:
    """使用 SQL 标准日期分量完成去重分组，兼顾生产 MySQL 与本地 SQLite 定向测试。"""

    parts: list[Any] = [extract("year", column), extract("month", column), extract("day", column)]
    if granularity == "hour":
        parts.append(extract("hour", column))
    return tuple(parts)


def _datetime_from_parts(parts: tuple[object, ...], current: datetime) -> datetime:
    """把 SQL 日期分量恢复为带当前业务时区的桶起点。"""

    year, month, day = (int(parts[index]) for index in range(3))
    hour = int(parts[3]) if len(parts) > 3 else 0
    return datetime(year, month, day, hour, tzinfo=current.tzinfo)


def _build_line_chart(items: list[dict[str, object]], series_keys: tuple[str, ...]) -> dict[str, object]:
    """为原生 SVG 生成稳定折线坐标，避免为只读大盘引入额外前端图表框架。"""

    maximum = max((int(item.get(key) or 0) for item in items for key in series_keys), default=0)
    point_map: dict[str, str] = {}
    item_count = len(items)
    for key in series_keys:
        points: list[str] = []
        for index, item in enumerate(items):
            value = max(int(item.get(key) or 0), 0)
            x = 2 if item_count <= 1 else round(2 + (index / (item_count - 1)) * 96, 2)
            y = 88 if maximum <= 0 else round(88 - (value / maximum) * 76, 2)
            points.append(f"{x},{y}")
        point_map[key] = " ".join(points)
    axis_labels = []
    if items:
        indexes = sorted({0, len(items) // 2, len(items) - 1})
        axis_labels = [str(items[index].get("label") or "") for index in indexes]
    return {"max_value": maximum, "points": point_map, "axis_labels": axis_labels}


def _channel_is_online(*, is_enabled: bool, status: str | None, login_state: str | None) -> bool:
    """渠道在线只读取通用启停、运行和登录字段，不依赖任何 Provider 专属状态。"""

    if not bool(is_enabled):
        return False
    return str(status or "").strip().lower() == "online" or str(login_state or "").strip().lower() == "online"


def _current_subscription(tenant: Tenant) -> TenantSubscription | None:
    """按开始时间和主键选出当前订阅，历史记录只用于审计而不重复计入大盘。"""

    subscriptions = list(tenant.subscriptions or ())
    if not subscriptions:
        return None
    return max(
        subscriptions,
        key=lambda item: (ensure_shanghai_datetime(item.starts_at), int(item.id or 0)),
    )


def _subscription_is_valid(subscription: TenantSubscription | None, current: datetime) -> bool:
    """有效订阅必须处于 active，且长期有效或尚未超过到期时间。"""

    if subscription is None or str(subscription.status or "") != "active":
        return False
    return subscription.expires_at is None or ensure_shanghai_datetime(subscription.expires_at) >= current


def _percentage(value: int, base: int) -> int:
    """转化率和使用率统一输出整数百分比，零基数不生成误导性数值。"""

    return 0 if base <= 0 else min(100, max(0, round((value / base) * 100)))


def _format_yuan(cents: int) -> str:
    """使用十进制定点数格式化平台审核金额，避免页面执行浮点运算。"""

    return f"{Decimal(int(cents)) / Decimal(100):,.2f}"


def build_platform_analytics(
    db: Session,
    *,
    range_key: str = "7d",
    now: datetime | None = None,
) -> dict[str, object]:
    """构建平台管理员可见的跨租户 SaaS 使用与经营大盘。

    聚合结果只包含平台级数量、租户名称和通用渠道类型，不返回消息正文、发送者标识、
    设备标识或凭证字段。调用方必须使用平台非租户会话，不能传入客户端 tenant_id。
    """

    normalized_range = normalize_analytics_range(range_key)
    current = ensure_shanghai_datetime(now or now_shanghai())
    range_context = _build_range_context(normalized_range, current)
    granularity = str(range_context["granularity"])
    buckets = list(range_context["buckets"])
    range_start = ensure_shanghai_datetime(range_context["start"])

    # 消息总量和趋势只读取小时聚合表。聚合缺失应作为数据质量问题显现为零，不能回退原始消息伪造第二套口径。
    message_rows = db.execute(
        select(
            MessageActivityHourly.bucket_start,
            MessageActivityHourly.tenant_id,
            MessageActivityHourly.direction,
            MessageActivityHourly.channel_type,
            func.sum(MessageActivityHourly.message_count),
        )
        .where(
            MessageActivityHourly.bucket_start >= range_start,
            MessageActivityHourly.bucket_start <= current,
        )
        .group_by(
            MessageActivityHourly.bucket_start,
            MessageActivityHourly.tenant_id,
            MessageActivityHourly.direction,
            MessageActivityHourly.channel_type,
        )
    ).all()

    message_by_bucket = {bucket: {"inbound": 0, "outbound": 0, "total": 0} for bucket in buckets}
    active_tenants_by_bucket: dict[datetime, set[int]] = {bucket: set() for bucket in buckets}
    tenant_message_totals: dict[int, int] = defaultdict(int)
    tenant_last_active: dict[int, datetime] = {}
    channel_message_totals: dict[str, int] = defaultdict(int)
    channel_last_active: dict[str, datetime] = {}
    active_tenant_ids: set[int] = set()
    inbound_total = 0
    outbound_total = 0

    for bucket_start, tenant_id, direction, channel_type, message_count in message_rows:
        count = max(int(message_count or 0), 0)
        if count <= 0:
            continue
        bucket = _bucket_datetime(bucket_start, granularity)
        if bucket not in message_by_bucket:
            continue
        normalized_direction = str(direction or "").strip().lower()
        direction_key = "outbound" if normalized_direction == "out" else "inbound"
        message_by_bucket[bucket][direction_key] += count
        message_by_bucket[bucket]["total"] += count
        normalized_tenant_id = int(tenant_id)
        active_tenant_ids.add(normalized_tenant_id)
        active_tenants_by_bucket[bucket].add(normalized_tenant_id)
        tenant_message_totals[normalized_tenant_id] += count
        exact_bucket = ensure_shanghai_datetime(bucket_start)
        if exact_bucket > tenant_last_active.get(normalized_tenant_id, range_start):
            tenant_last_active[normalized_tenant_id] = exact_bucket
        normalized_channel_type = str(channel_type or "unknown").strip() or "unknown"
        channel_message_totals[normalized_channel_type] += count
        if exact_bucket > channel_last_active.get(normalized_channel_type, range_start):
            channel_last_active[normalized_channel_type] = exact_bucket
        if direction_key == "outbound":
            outbound_total += count
        else:
            inbound_total += count

    # 触发用户按租户和发送者去重，并在时间桶内再次去重；只选择标识做计数，结果不会带到模板。
    time_parts = _time_group_expressions(Message.created_at, granularity)
    trigger_rows = db.execute(
        select(*time_parts, Message.tenant_id, Message.sender_id)
        .where(
            Message.created_at >= range_start,
            Message.created_at <= current,
            Message.direction == "in",
            Message.sender_id.is_not(None),
            Message.sender_id != "",
        )
        .group_by(*time_parts, Message.tenant_id, Message.sender_id)
    ).all()
    trigger_users_by_bucket: dict[datetime, set[tuple[int, str]]] = {bucket: set() for bucket in buckets}
    tenant_trigger_users: dict[int, set[str]] = defaultdict(set)
    all_trigger_users: set[tuple[int, str]] = set()
    part_count = len(time_parts)
    for row in trigger_rows:
        bucket = _datetime_from_parts(tuple(row[:part_count]), current)
        if bucket not in trigger_users_by_bucket:
            continue
        tenant_id = int(row[part_count])
        sender_id = str(row[part_count + 1] or "")
        if not sender_id:
            continue
        pair = (tenant_id, sender_id)
        trigger_users_by_bucket[bucket].add(pair)
        tenant_trigger_users[tenant_id].add(sender_id)
        all_trigger_users.add(pair)

    message_trend_items: list[dict[str, object]] = []
    activity_trend_items: list[dict[str, object]] = []
    for bucket in buckets:
        message_values = message_by_bucket[bucket]
        message_trend_items.append(
            {
                "bucket": bucket,
                "label": _bucket_label(bucket, granularity),
                "inbound": int(message_values["inbound"]),
                "outbound": int(message_values["outbound"]),
                "total": int(message_values["total"]),
            }
        )
        activity_trend_items.append(
            {
                "bucket": bucket,
                "label": _bucket_label(bucket, granularity),
                "trigger_users": len(trigger_users_by_bucket[bucket]),
                "active_tenants": len(active_tenants_by_bucket[bucket]),
            }
        )

    tenants = db.scalars(
        select(Tenant)
        .options(selectinload(Tenant.subscriptions).selectinload(TenantSubscription.plan))
        .order_by(Tenant.id.asc())
    ).all()
    tenant_map = {int(tenant.id): tenant for tenant in tenants}
    tenant_ids = set(tenant_map)
    current_subscriptions = {tenant_id: _current_subscription(tenant) for tenant_id, tenant in tenant_map.items()}
    valid_subscription_ids = {
        tenant_id
        for tenant_id, subscription in current_subscriptions.items()
        if _subscription_is_valid(subscription, current)
    }

    binding_rows = db.execute(
        select(BotBinding.tenant_id, BotBinding.status).where(BotBinding.status.in_(("active", "disabled")))
    ).all()
    account_totals: dict[int, int] = defaultdict(int)
    for tenant_id, _status in binding_rows:
        account_totals[int(tenant_id)] += 1
    bound_tenant_ids = set(account_totals)

    channel_rows = db.execute(
        select(Channel.tenant_id, Channel.channel_type, Channel.is_enabled, Channel.status, Channel.login_state).where(Channel.deleted_at.is_(None))
    ).all()
    channel_totals: dict[int, int] = defaultdict(int)
    online_channel_totals: dict[int, int] = defaultdict(int)
    channel_type_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "online": 0})
    enabled_channel_total = 0
    online_channel_total = 0
    for tenant_id, channel_type, is_enabled, status, login_state in channel_rows:
        normalized_tenant_id = int(tenant_id)
        normalized_channel_type = str(channel_type or "unknown").strip() or "unknown"
        online = _channel_is_online(is_enabled=bool(is_enabled), status=status, login_state=login_state)
        channel_totals[normalized_tenant_id] += 1
        channel_type_counts[normalized_channel_type]["total"] += 1
        if bool(is_enabled):
            enabled_channel_total += 1
        if online:
            online_channel_total += 1
            online_channel_totals[normalized_tenant_id] += 1
            channel_type_counts[normalized_channel_type]["online"] += 1
    online_tenant_ids = set(online_channel_totals)

    pending_order_total = int(
        db.scalar(select(func.count(BillingOrder.id)).where(BillingOrder.status == "payment_submitted")) or 0
    )
    approved_amount_cents = int(
        db.scalar(
            select(func.sum(BillingOrder.amount_cents)).where(
                BillingOrder.status == "approved",
                BillingOrder.reviewed_at.is_not(None),
                BillingOrder.reviewed_at >= range_start,
                BillingOrder.reviewed_at <= current,
            )
        )
        or 0
    )

    message_total = inbound_total + outbound_total
    core_metrics = {
        "tenant_total": len(tenant_ids),
        "active_tenant_total": len(active_tenant_ids),
        "message_total": message_total,
        "inbound_total": inbound_total,
        "outbound_total": outbound_total,
        "trigger_user_total": len(all_trigger_users),
        "connected_account_total": sum(account_totals.values()),
        "online_channel_total": online_channel_total,
        "channel_online_rate": _percentage(online_channel_total, enabled_channel_total),
        "pending_order_total": pending_order_total,
    }
    core_metric_items = [
        {"label": "租户总数", "value": core_metrics["tenant_total"], "note": "当前平台规模"},
        {"label": "活跃租户", "value": core_metrics["active_tenant_total"], "note": f"近 {range_context['label']}"},
        {"label": "消息总数", "value": core_metrics["message_total"], "note": "入站与出站合计"},
        {"label": "入站消息", "value": core_metrics["inbound_total"], "note": "用户触发消息"},
        {"label": "出站消息", "value": core_metrics["outbound_total"], "note": "机器人发送消息"},
        {"label": "触发用户", "value": core_metrics["trigger_user_total"], "note": "按租户内用户去重"},
        {"label": "接入账号", "value": core_metrics["connected_account_total"], "note": "未释放机器人账号"},
        {"label": "在线渠道", "value": core_metrics["online_channel_total"], "note": "当前通用渠道状态"},
        {"label": "渠道在线率", "value": f"{core_metrics['channel_online_rate']}%", "note": "当前启用渠道"},
        {"label": "待审核订单", "value": core_metrics["pending_order_total"], "note": "当前平台待办"},
    ]

    # 使用漏斗按上一层集合继续收敛，避免当前离线但历史有消息的租户导致后层数量反向增长。
    valid_funnel_ids = tenant_ids & valid_subscription_ids
    bound_funnel_ids = valid_funnel_ids & bound_tenant_ids
    online_funnel_ids = bound_funnel_ids & online_tenant_ids
    messaged_funnel_ids = online_funnel_ids & active_tenant_ids
    funnel_layers = [
        ("已注册租户", tenant_ids),
        ("当前有效订阅", valid_funnel_ids),
        ("已绑定机器人账号", bound_funnel_ids),
        ("至少一个渠道在线", online_funnel_ids),
        ("周期内产生消息", messaged_funnel_ids),
    ]
    adoption_funnel: list[dict[str, object]] = []
    previous_count = 0
    for index, (label, values) in enumerate(funnel_layers):
        count = len(values)
        rate = 100 if index == 0 and count > 0 else _percentage(count, previous_count)
        adoption_funnel.append(
            {
                "step": index + 1,
                "label": label,
                "count": count,
                "conversion_rate": rate,
                "share_rate": _percentage(count, len(tenant_ids)),
            }
        )
        previous_count = count

    channel_summary: list[dict[str, object]] = []
    for channel_type, counts in channel_type_counts.items():
        channel_summary.append(
            {
                "channel_type": channel_type,
                "channel_label": get_channel_type_label(channel_type),
                "account_total": int(counts["total"]),
                "online_total": int(counts["online"]),
                "abnormal_total": max(0, int(counts["total"]) - int(counts["online"])),
                "message_total": int(channel_message_totals.get(channel_type, 0)),
                "last_active_at": channel_last_active.get(channel_type),
            }
        )
    channel_summary.sort(key=lambda item: (-int(item["message_total"]), str(item["channel_type"])))

    tenant_rankings: list[dict[str, object]] = []
    for tenant_id, message_count in tenant_message_totals.items():
        tenant = tenant_map.get(tenant_id)
        if tenant is None or message_count <= 0:
            continue
        subscription = current_subscriptions.get(tenant_id)
        plan = subscription.plan if subscription is not None else None
        bot_limit = max(int(getattr(plan, "bot_limit", 0) or 0), 0)
        used_accounts = int(account_totals.get(tenant_id, 0))
        tenant_rankings.append(
            {
                "tenant_id": tenant_id,
                "tenant_name": tenant.name,
                "plan_name": getattr(plan, "name", None) or "未订阅",
                "subscription_status": "active"
                if _subscription_is_valid(subscription, current)
                else str(getattr(subscription, "status", "none") or "none"),
                "message_total": int(message_count),
                "trigger_user_total": len(tenant_trigger_users.get(tenant_id, set())),
                "account_total": used_accounts,
                "online_channel_total": int(online_channel_totals.get(tenant_id, 0)),
                "quota_usage_rate": _percentage(used_accounts, bot_limit),
                "quota_text": f"{used_accounts}/{bot_limit}" if bot_limit > 0 else str(used_accounts),
                "last_active_at": tenant_last_active.get(tenant_id),
            }
        )
    tenant_rankings.sort(
        key=lambda item: (
            -int(item["message_total"]),
            -int(item["trigger_user_total"]),
            str(item["tenant_name"]),
        )
    )
    tenant_rankings = tenant_rankings[:10]

    health_definitions = {
        "subscription_inactive": (1, "订阅已失效", "danger"),
        "quota_full": (2, "机器人额度已用满", "warning"),
        "account_missing": (3, "尚未绑定账号", "warning"),
        "channel_offline": (4, "没有在线渠道", "warning"),
        "message_inactive": (5, "周期内没有消息", "muted"),
        "subscription_expiring": (6, "订阅将在 7 天内到期", "muted"),
    }
    tenant_health: list[dict[str, object]] = []
    expiring_deadline = current + timedelta(days=7)
    for tenant_id, tenant in tenant_map.items():
        subscription = current_subscriptions.get(tenant_id)
        valid = _subscription_is_valid(subscription, current)
        plan = subscription.plan if subscription is not None else None
        bot_limit = max(int(getattr(plan, "bot_limit", 0) or 0), 0)
        used_accounts = int(account_totals.get(tenant_id, 0))
        status = ""
        detail = ""
        if not valid:
            status = "subscription_inactive"
            detail = "续费或恢复订阅后才能继续使用"
        elif bot_limit > 0 and used_accounts >= bot_limit:
            status = "quota_full"
            detail = f"已使用 {used_accounts}/{bot_limit} 个机器人额度"
        elif used_accounts <= 0:
            status = "account_missing"
            detail = "尚未完成首个机器人账号接入"
        elif int(online_channel_totals.get(tenant_id, 0)) <= 0:
            status = "channel_offline"
            detail = "已接入账号当前均未在线"
        elif int(tenant_message_totals.get(tenant_id, 0)) <= 0:
            status = "message_inactive"
            detail = f"近 {range_context['label']} 未产生消息"
        elif subscription and subscription.expires_at is not None and ensure_shanghai_datetime(subscription.expires_at) <= expiring_deadline:
            status = "subscription_expiring"
            detail = "需要提前跟进续费"
        if not status:
            continue
        priority, label, tone = health_definitions[status]
        tenant_health.append(
            {
                "tenant_id": tenant_id,
                "tenant_name": tenant.name,
                "status": status,
                "status_label": label,
                "tone": tone,
                "detail": detail,
                "priority": priority,
            }
        )
    tenant_health.sort(key=lambda item: (int(item["priority"]), str(item["tenant_name"])))
    tenant_health = tenant_health[:12]

    valid_subscriptions = [
        subscription
        for tenant_id, subscription in current_subscriptions.items()
        if tenant_id in valid_subscription_ids and subscription is not None
    ]
    trial_tenant_total = sum(
        1 for subscription in valid_subscriptions if int(getattr(subscription.plan, "trial_days", 0) or 0) > 0
    )
    paid_tenant_total = sum(
        1
        for subscription in valid_subscriptions
        if max(
            int(getattr(subscription.plan, "monthly_price_cents", 0) or 0),
            int(getattr(subscription.plan, "yearly_price_cents", 0) or 0),
        )
        > 0
    )
    expiring_soon_total = sum(
        1
        for subscription in valid_subscriptions
        if subscription.expires_at is not None
        and current <= ensure_shanghai_datetime(subscription.expires_at) <= expiring_deadline
    )
    subscription_summary = {
        "trial_tenant_total": trial_tenant_total,
        "paid_tenant_total": paid_tenant_total,
        "expiring_soon_total": expiring_soon_total,
        "inactive_total": len(tenant_ids - valid_subscription_ids),
        "pending_order_total": pending_order_total,
        "approved_amount_cents": approved_amount_cents,
        "approved_amount_yuan": _format_yuan(approved_amount_cents),
    }

    session_rows = db.execute(
        select(
            TenantSession.tenant_user_id,
            TenantSession.expires_at,
            TenantSession.last_seen_at,
            TenantSession.revoked_at,
        )
    ).all()
    valid_session_total = 0
    active_employee_ids: set[int] = set()
    active_employee_cutoff = current - timedelta(minutes=30)
    for user_id, expires_at, last_seen_at, revoked_at in session_rows:
        valid_session = revoked_at is None and ensure_shanghai_datetime(expires_at) >= current
        if not valid_session:
            continue
        valid_session_total += 1
        if ensure_shanghai_datetime(last_seen_at) >= active_employee_cutoff:
            active_employee_ids.add(int(user_id))

    executor_rows = db.execute(select(ProviderNode.status, ProviderNode.last_heartbeat_at)).all()
    executor_cutoff = current - timedelta(minutes=5)
    online_executor_total = sum(
        1
        for status, last_heartbeat_at in executor_rows
        if str(status or "") == "active"
        and last_heartbeat_at is not None
        and ensure_shanghai_datetime(last_heartbeat_at) >= executor_cutoff
    )
    presence_summary = {
        "active_employee_total": len(active_employee_ids),
        "valid_session_total": valid_session_total,
        "online_executor_total": online_executor_total,
        "online_channel_total": online_channel_total,
        "abnormal_channel_total": max(0, enabled_channel_total - online_channel_total),
    }

    range_options = [
        {
            "key": key,
            "label": str(config["label"]),
            "active": key == normalized_range,
            "href": f"/platform/dashboard?range={key}",
        }
        for key, config in ANALYTICS_RANGES.items()
    ]
    return {
        "range": normalized_range,
        "range_label": str(range_context["label"]),
        "range_start": range_start,
        "range_end": current,
        "range_options": range_options,
        "core_metrics": core_metrics,
        "core_metric_items": core_metric_items,
        "message_trend": {
            "items": message_trend_items,
            "chart": _build_line_chart(message_trend_items, ("inbound", "outbound")),
        },
        "activity_trend": {
            "items": activity_trend_items,
            "chart": _build_line_chart(activity_trend_items, ("trigger_users", "active_tenants")),
        },
        "adoption_funnel": adoption_funnel,
        "channel_summary": channel_summary,
        "tenant_rankings": tenant_rankings,
        "tenant_health": tenant_health,
        "subscription_summary": subscription_summary,
        "presence_summary": presence_summary,
    }
