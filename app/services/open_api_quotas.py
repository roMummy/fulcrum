"""开放接口套餐额度和月度使用账本。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import OpenApiApp, OpenApiAppChannel, OpenApiUsageRecord
from app.models.saas import SaasPlan, TenantSubscription
from app.services.billing import get_current_subscription, subscription_is_active


OPEN_API_METRICS = frozenset({"api_requests", "messages", "media_bytes"})
_PLAN_LIMIT_FIELDS = {
    "api_requests": "monthly_api_request_limit",
    "messages": "monthly_message_limit",
    "media_bytes": "monthly_media_bytes_limit",
}


class OpenApiQuotaError(ValueError):
    """开放接口订阅或额度不满足时返回的稳定错误码。"""

    def __init__(self, code: str, *, metric: str = ""):
        super().__init__(code)
        self.code = code
        self.metric = metric


@dataclass(frozen=True)
class OpenApiUsageReservation:
    """一次请求的额度预扣结果。"""

    records: tuple[OpenApiUsageRecord, ...]
    idempotent: bool
    period: str


def _current_period(value: datetime | None = None) -> str:
    """以北京时间生成自然月账期，账本不会受服务器 UTC 时区影响。"""

    current = ensure_shanghai_datetime(value) if value is not None else now_shanghai()
    return current.strftime("%Y-%m")


def _active_plan(db: Session, *, tenant_id: int) -> tuple[TenantSubscription, SaasPlan]:
    """读取当前租户有效订阅和套餐，并统一拒绝过期订阅。"""

    subscription = get_current_subscription(db, tenant_id=int(tenant_id))
    if not subscription_is_active(subscription):
        raise OpenApiQuotaError("subscription_required")
    plan = subscription.plan
    if plan is None or plan.status != "active":
        raise OpenApiQuotaError("subscription_required")
    return subscription, plan


def plan_quota_summary(db: Session, *, tenant_id: int) -> dict[str, int | str]:
    """返回当前订阅可对外展示的额度快照。0 额度在首期表示不设上限。"""

    _subscription, plan = _active_plan(db, tenant_id=int(tenant_id))
    return {
        "plan_code": plan.code,
        "open_api_app_limit": int(plan.open_api_app_limit or 0),
        "open_api_channel_limit": int(plan.open_api_channel_limit or 0),
        "monthly_api_request_limit": int(plan.monthly_api_request_limit or 0),
        "monthly_message_limit": int(plan.monthly_message_limit or 0),
        "monthly_media_bytes_limit": int(plan.monthly_media_bytes_limit or 0),
    }


def usage_summary(db: Session, *, tenant_id: int, period: str | None = None) -> dict[str, int | str]:
    """汇总账期内已预扣、已确认的用量和剩余额度。"""

    _subscription, plan = _active_plan(db, tenant_id=int(tenant_id))
    current_period = period or _current_period()
    rows = db.execute(
        select(OpenApiUsageRecord.metric, func.coalesce(func.sum(OpenApiUsageRecord.units), 0))
        .where(
            OpenApiUsageRecord.tenant_id == int(tenant_id),
            OpenApiUsageRecord.period == current_period,
            OpenApiUsageRecord.status.in_(("reserved", "committed")),
        )
        .group_by(OpenApiUsageRecord.metric)
    ).all()
    used = {str(metric): int(total or 0) for metric, total in rows}
    result: dict[str, int | str] = {"period": current_period}
    for metric, field in _PLAN_LIMIT_FIELDS.items():
        limit = int(getattr(plan, field) or 0)
        amount = used.get(metric, 0)
        result[f"{metric}_used"] = amount
        result[f"{metric}_limit"] = limit
        result[f"{metric}_remaining"] = 0 if limit == 0 else max(0, limit - amount)
    return result


def require_open_api_resource_quota(
    db: Session,
    *,
    tenant_id: int,
    app_id: int | None,
    requested_channel_ids: set[int],
) -> None:
    """校验开放应用数和授权渠道数，不修改数据库。

    更新应用时会排除该应用旧的授权行，再把本次提交的目标集合计入；这样
    管理员缩小授权范围不会因为旧记录而误报额度不足。
    """

    _subscription, plan = _active_plan(db, tenant_id=int(tenant_id))
    target_app = db.get(OpenApiApp, int(app_id)) if app_id is not None else None
    if target_app is not None and int(target_app.tenant_id) != int(tenant_id):
        raise OpenApiQuotaError("app_not_found")

    if app_id is None:
        active_apps = db.scalar(
            select(func.count(OpenApiApp.id)).where(
                OpenApiApp.tenant_id == int(tenant_id),
                OpenApiApp.status == "active",
            )
        ) or 0
        if int(plan.open_api_app_limit or 0) > 0 and int(active_apps) >= int(plan.open_api_app_limit):
            raise OpenApiQuotaError("quota_exceeded", metric="open_api_apps")

    channel_query = select(func.count(OpenApiAppChannel.id)).where(
        OpenApiAppChannel.tenant_id == int(tenant_id)
    )
    if app_id is not None:
        channel_query = channel_query.where(OpenApiAppChannel.app_id != int(app_id))
    existing_channels = int(db.scalar(channel_query) or 0)
    total_channels = existing_channels + len({int(item) for item in requested_channel_ids if int(item) > 0})
    if int(plan.open_api_channel_limit or 0) > 0 and total_channels > int(plan.open_api_channel_limit):
        raise OpenApiQuotaError("quota_exceeded", metric="open_api_channels")


def _reservation_key(*, app_id: int, period: str, metric: str, request_id: str) -> str:
    """构造全局唯一幂等键，避免不同应用或不同月份碰撞。"""

    return f"open-api:{int(app_id)}:{period}:{metric}:{request_id}"


def reserve_open_api_usage(
    db: Session,
    *,
    app: OpenApiApp,
    request_id: str,
    usage: dict[str, int],
    metadata: dict[str, object] | None = None,
) -> OpenApiUsageReservation:
    """在当前事务中预扣 API、消息或媒体额度。

    已存在的 reservation_key 会原样返回，调用方可以安全处理网络重试；新记录
    使用 ``flush`` 而不是 ``commit``，由消息任务和业务 API 统一决定事务边界。
    """

    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        raise OpenApiQuotaError("request_id_required")
    normalized_usage = {str(metric): int(amount) for metric, amount in usage.items() if int(amount) > 0}
    unknown = set(normalized_usage) - OPEN_API_METRICS
    if unknown:
        raise OpenApiQuotaError("metric_invalid")
    if not normalized_usage:
        return OpenApiUsageReservation(records=(), idempotent=False, period=_current_period())

    tenant_id = int(app.tenant_id)
    _subscription, plan = _active_plan(db, tenant_id=tenant_id)
    period = _current_period()
    existing_records: list[OpenApiUsageRecord] = []
    new_metrics: dict[str, int] = {}
    for metric, amount in normalized_usage.items():
        key = _reservation_key(app_id=int(app.id), period=period, metric=metric, request_id=normalized_request_id)
        existing = db.scalar(select(OpenApiUsageRecord).where(OpenApiUsageRecord.reservation_key == key))
        if existing is not None:
            existing_records.append(existing)
            continue
        new_metrics[metric] = amount

    if existing_records and len(existing_records) != len(normalized_usage):
        # 同一请求只允许完整地预扣一次；部分存在通常说明上次事务被外部中断，
        # 这里拒绝继续追加，避免业务系统得到不完整的额度状态。
        raise OpenApiQuotaError("reservation_conflict")
    if existing_records:
        return OpenApiUsageReservation(records=tuple(existing_records), idempotent=True, period=period)

    current_usage = usage_summary(db, tenant_id=tenant_id, period=period)
    for metric, amount in new_metrics.items():
        limit_field = _PLAN_LIMIT_FIELDS[metric]
        limit = int(getattr(plan, limit_field) or 0)
        used = int(current_usage[f"{metric}_used"])
        if limit > 0 and used + amount > limit:
            raise OpenApiQuotaError("quota_exceeded", metric=metric)

    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
    records: list[OpenApiUsageRecord] = []
    for metric, amount in new_metrics.items():
        record = OpenApiUsageRecord(
            app_id=int(app.id),
            period=period,
            metric=metric,
            request_id=normalized_request_id,
            reservation_key=_reservation_key(
                app_id=int(app.id), period=period, metric=metric, request_id=normalized_request_id
            ),
            units=amount,
            status="reserved",
            metadata_json=metadata_json,
        )
        db.add(record)
        records.append(record)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise OpenApiQuotaError("reservation_conflict") from exc
    return OpenApiUsageReservation(records=tuple(records), idempotent=False, period=period)


def finalize_open_api_usage(db: Session, *, reservation: OpenApiUsageReservation, status: str) -> None:
    """把预扣记录确认为已使用或可返还。"""

    if status not in {"committed", "refunded"}:
        raise OpenApiQuotaError("usage_status_invalid")
    timestamp = now_shanghai()
    for record in reservation.records:
        if record.status == "reserved":
            record.status = status
            record.finalized_at = timestamp

