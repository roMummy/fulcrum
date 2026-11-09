from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    Message,
    PluginStateRecord,
    PointsAccount,
    TaskJob,
)
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore, parse_state_payload


PLUGIN_CODE = "value_rank"
SNAPSHOT_NAMESPACE = "value_rank_snapshots"
WEEKLY_REPORT_NAMESPACE = "value_rank_weekly_reports"
DAILY_ACTION = "value_rank_daily_recompute"
WEEKLY_PREPARE_ACTION = "value_rank_weekly_report_prepare"
WEEKLY_PUSH_ACTION = "value_rank_weekly_report_push"
SCHEDULE_ACTIONS = {DAILY_ACTION, WEEKLY_PREPARE_ACTION, WEEKLY_PUSH_ACTION}
COMMANDS = (
    "我的身价",
    "身价排行",
    "社交热度榜",
    "搭子榜",
    "社交桥梁榜",
    "社交关系图",
    "我的趋势",
    "身价周报",
    "身价说明",
    "重算身价",
)


@dataclass(frozen=True)
class ValueRankConfig:
    """保存一个租户绑定唯一生效的统计口径、调度和权限参数。"""

    message_window_days: int
    active_window_days: int
    social_window_days: int
    points_weight: float
    message_weight: float
    active_days_weight: float
    social_weight: float
    inactivity_penalty_max: float
    base_score_scale: float
    default_rank_limit: int
    max_rank_limit: int
    default_trend_days: int
    max_trend_days: int
    snapshot_retention_days: int
    weekly_report_retention_days: int
    administrator_user_ids: frozenset[str]


@dataclass(frozen=True)
class TrustedValueGroup:
    """保存经过数据库归属校验的群、渠道和插件绑定。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    chatroom: Chatroom
    message: Message | None = None
    sender: ChatroomMember | None = None


def matches_event(context) -> dict[str, object]:
    """只匹配群文本中的十个当前身价命令。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    command, _argument = _parse_command(str(getattr(context, "content", "") or ""))
    matched = chat_type == "group" and message_type == "text" and command in COMMANDS
    return {
        "matched": matched,
        "reason": "value_rank_hit" if matched else "value_rank_command_required",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只报告配置状态，业务统计必须来自真实群消息或统一调度。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群成员身价插件已就绪。",
            output={
                "commands": list(COMMANDS),
                "message_window_days": config.message_window_days,
                "social_window_days": config.social_window_days,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在群聊中发送身价查询命令。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """从真实群消息解析命令，并使用当前绑定的结构化快照生成业务视图。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    trusted, error = _require_message_group(db, request.context)
    if error is not None or trusted is None or trusted.message is None or trusted.sender is None:
        return error or _error_result("当前群聊消息上下文无效。", "trusted_context_required")

    command, argument = _parse_command(str(trusted.message.content or ""))
    if command not in COMMANDS:
        return _error_result("当前消息不是身价系统命令。", "command_invalid")
    if command == "身价说明":
        return _text_result(command, _format_explanation(config), snapshot_member_count=0)
    if command == "重算身价":
        sender_id = str(trusted.sender.member_external_id or "").strip()
        if sender_id not in config.administrator_user_ids:
            return _error_result("只有租户配置的身价管理员可以执行重算。", "administrator_required")
        snapshot = _recompute_snapshot(db, trusted, config)
        return _text_result(
            command,
            f"身价重算完成，已更新 {len(snapshot['rows'])} 位群成员。",
            provider_status="recomputed",
            snapshot_member_count=len(snapshot["rows"]),
        )

    snapshot = _ensure_snapshot(db, trusted, config)
    if not snapshot.get("rows"):
        return _error_result("当前群暂无可用身价数据。", "snapshot_empty")
    sender_id = str(trusted.sender.member_external_id or "").strip()
    limit = _parse_bounded_argument(argument, config.default_rank_limit, config.max_rank_limit)
    if command == "我的身价":
        text = _format_my_value(snapshot, sender_id, db=db, trusted=trusted)
    elif command == "身价排行":
        text = _format_rankings(snapshot, limit)
    elif command == "社交热度榜":
        text = _format_social_hot(snapshot, limit)
    elif command == "搭子榜":
        text = _format_partners(snapshot, limit)
    elif command == "社交桥梁榜":
        text = _format_bridges(snapshot, limit)
    elif command == "社交关系图":
        text = _format_social_graph(snapshot, limit)
    elif command == "我的趋势":
        days = _parse_bounded_argument(argument, config.default_trend_days, config.max_trend_days)
        text = _format_trend(db, trusted, sender_id, days)
    elif command == "身价周报":
        report = _build_weekly_report(db, trusted, config, snapshot=snapshot)
        text = _format_weekly_report(report)
    else:
        return _error_result("当前身价命令暂未实现。", "command_unsupported")
    if not text:
        return _error_result("当前数据不足，暂时无法生成该视图。", "data_insufficient")
    return _text_result(
        command,
        text,
        snapshot_member_count=len(snapshot["rows"]),
    )


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """为一个可信群重算快照、预生成周报或幂等创建周报出站任务。"""

    if request.action not in SCHEDULE_ACTIONS:
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的身价调度动作: {request.action}")
    config = _load_config(request.config)
    trusted, error = _require_scheduled_group(db, request.context)
    if error is not None or trusted is None:
        return error or _error_result("身价任务缺少可信目标群。", "trusted_context_required")
    snapshot = _recompute_snapshot(db, trusted, config)
    if request.action == DAILY_ACTION:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=f"已重算 {len(snapshot['rows'])} 位群成员身价。",
            output={
                "provider_status": "completed",
                "conversation_id": int(trusted.conversation.id),
                "snapshot_member_count": len(snapshot["rows"]),
            },
        )

    report = _build_weekly_report(db, trusted, config, snapshot=snapshot)
    store = PluginStateStore(db, trusted.execution)
    report_key = _weekly_report_key(int(trusted.conversation.id), str(report["week_key"]))
    store.put(
        WEEKLY_REPORT_NAMESPACE,
        report_key,
        report,
        ttl_seconds=config.weekly_report_retention_days * 24 * 60 * 60,
        now=now_shanghai(),
    )
    if request.action == WEEKLY_PREPARE_ACTION:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="身价周报已预生成。",
            output={
                "provider_status": "completed",
                "conversation_id": int(trusted.conversation.id),
                "week_key": report["week_key"],
            },
        )

    text = _format_weekly_report(report)
    dedupe_key = (
        f"value-rank-weekly:{int(trusted.binding.id)}:{int(trusted.conversation.id)}:"
        f"{report['week_key']}:push"
    )
    existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
    if existing is None:
        try:
            with db.begin_nested():
                existing = enqueue_outbound_job(
                    db,
                    conversation=trusted.conversation,
                    channel=trusted.channel,
                    payload={
                        "external_id": str(trusted.conversation.external_id),
                        "receiver_id": str(trusted.conversation.external_id),
                        "chat_type": "group",
                        "message_type": "text",
                        "content": text,
                    },
                    plugin_code=PLUGIN_CODE,
                    related_type="value_rank_binding",
                    related_id=int(trusted.binding.id),
                    priority=70,
                    dedupe_key=dedupe_key,
                    max_attempts=3,
                )
        except IntegrityError:
            existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
            if existing is None:
                raise
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="身价周报已进入发送队列。",
        output={
            "provider_status": "completed",
            "conversation_id": int(trusted.conversation.id),
            "week_key": report["week_key"],
            "outbound_job_id": int(existing.id),
        },
    )


def _require_message_group(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedValueGroup | None, PluginExecutionResult | None]:
    """验证真实群消息、发送成员和插件绑定归属。"""

    trusted, error = _require_group_base(db, context, require_message=True)
    if error is not None or trusted is None or trusted.message is None:
        return trusted, error
    sender_id = str(trusted.message.sender_id or "").strip()
    sender = db.scalar(
        select(ChatroomMember).where(
            ChatroomMember.chatroom_id == int(trusted.chatroom.id),
            ChatroomMember.member_external_id == sender_id,
            ChatroomMember.status == "active",
        )
    )
    if sender is None:
        return None, _error_result("当前无法确认发送人是本群活跃成员。", "group_member_required")
    return TrustedValueGroup(
        trusted.execution,
        trusted.binding,
        trusted.channel,
        trusted.conversation,
        trusted.chatroom,
        trusted.message,
        sender,
    ), None


def _require_scheduled_group(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedValueGroup | None, PluginExecutionResult | None]:
    """验证统一调度器重建的绑定、渠道和群会话归属。"""

    return _require_group_base(db, context, require_message=False)


def _require_group_base(
    db: Session,
    context: PluginExecutionContext | None,
    *,
    require_message: bool,
) -> tuple[TrustedValueGroup | None, PluginExecutionResult | None]:
    """为命令和调度共享同一条可信群定位路径。"""

    if context is None or context.channel_id is None or context.conversation_id is None:
        return None, _error_result("身价功能缺少目标群聊。", "trusted_context_required")
    if require_message and context.message_id is None:
        return None, _error_result("身价命令缺少真实入站消息。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id)) if context.message_id is not None else None
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or plugin_code != PLUGIN_CODE
        or channel is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or not channel.is_enabled
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or str(conversation.chat_type or "") != "group"
    ):
        return None, _error_result("当前身价群聊上下文无效。", "trusted_context_required")
    if require_message and (
        message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "") != "in"
        or str(message.message_type or "") != "text"
        or not str(message.sender_id or "").strip()
        or (context.sender_id is not None and str(context.sender_id).strip() != str(message.sender_id).strip())
    ):
        return None, _error_result("当前身价消息上下文无效。", "trusted_context_required")
    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(channel.id),
            Chatroom.external_id == str(conversation.external_id),
            Chatroom.is_active.is_(True),
        )
    )
    if chatroom is None:
        return None, _error_result("当前群尚未完成成员同步。", "chatroom_unavailable")
    return TrustedValueGroup(context, binding, channel, conversation, chatroom, message, None), None


def _ensure_snapshot(db: Session, trusted: TrustedValueGroup, config: ValueRankConfig) -> dict[str, Any]:
    """读取今日快照；不存在时只重算当前群一次。"""

    date_key = now_shanghai().date().isoformat()
    store = PluginStateStore(db, trusted.execution)
    cached = store.get(SNAPSHOT_NAMESPACE, _snapshot_key(int(trusted.conversation.id), date_key), now=now_shanghai())
    if cached is not None and int(cached.get("conversation_id") or 0) == int(trusted.conversation.id):
        return cached
    return _recompute_snapshot(db, trusted, config)


def _recompute_snapshot(db: Session, trusted: TrustedValueGroup, config: ValueRankConfig) -> dict[str, Any]:
    """聚合当前群指标、计算归一化身价并覆盖当天结构化快照。"""

    current = now_shanghai()
    message_cutoff = current - timedelta(days=config.message_window_days)
    active_cutoff = current - timedelta(days=config.active_window_days)
    social_cutoff = current - timedelta(days=config.social_window_days)
    members = db.scalars(
        select(ChatroomMember).where(
            ChatroomMember.chatroom_id == int(trusted.chatroom.id),
            ChatroomMember.status == "active",
        )
    ).all()
    member_names = {
        str(member.member_external_id): str(member.display_name or member.nickname or member.member_external_id).strip()
        for member in members
        if str(member.member_external_id or "").strip()
    }
    points_rows = db.scalars(
        select(PointsAccount).where(PointsAccount.group_id == str(trusted.conversation.external_id))
    ).all()
    points_map = {str(row.user_id): int(row.total_points or 0) for row in points_rows}
    all_messages = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == int(trusted.conversation.id),
            Message.direction == "in",
            Message.message_type == "text",
            Message.sender_id.is_not(None),
            func.trim(Message.content) != "",
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
    ).all()

    message_counts: Counter[str] = Counter()
    active_days: dict[str, set[date]] = defaultdict(set)
    last_active: dict[str, datetime] = {}
    candidate_ids = set(points_map)
    for message in all_messages:
        sender_id = str(message.sender_id or "").strip()
        if not sender_id:
            continue
        created_at = ensure_shanghai_datetime(message.created_at)
        candidate_ids.add(sender_id)
        previous = last_active.get(sender_id)
        if previous is None or created_at > previous:
            last_active[sender_id] = created_at
        if created_at >= message_cutoff:
            message_counts[sender_id] += 1
        if created_at >= active_cutoff:
            active_days[sender_id].add(created_at.date())

    # 社交关系只识别当前活跃群成员的明确 @账号、@昵称或 @展示名。解析结果
    # 完全来自平台标准文本，不读取微信 XML 或 Provider 私有提及字段。
    alias_map: dict[str, set[str]] = defaultdict(set)
    for member in members:
        member_id = str(member.member_external_id or "").strip()
        if not member_id:
            continue
        for alias in (member_id, member.display_name, member.nickname):
            normalized = str(alias or "").strip()
            if normalized:
                alias_map[normalized].add(member_id)
    directed_edges: Counter[tuple[str, str]] = Counter()
    for message in all_messages:
        created_at = ensure_shanghai_datetime(message.created_at)
        sender_id = str(message.sender_id or "").strip()
        if created_at < social_cutoff or not sender_id:
            continue
        content = str(message.content or "")
        targets: set[str] = set()
        for alias, member_ids in alias_map.items():
            if f"@{alias}" in content and len(member_ids) == 1:
                target_id = next(iter(member_ids))
                if target_id != sender_id:
                    targets.add(target_id)
        for target_id in targets:
            directed_edges[(sender_id, target_id)] += 1
            candidate_ids.add(target_id)

    social_stats, partners, bridges = _build_social_stats(directed_edges, member_names)
    candidate_ids = {user_id for user_id in candidate_ids if user_id in member_names or user_id in points_map}
    metrics: list[dict[str, Any]] = []
    for user_id in sorted(candidate_ids):
        last = last_active.get(user_id)
        inactive_days = max((current.date() - last.date()).days, 0) if last is not None else 365
        metrics.append(
            {
                "user_id": user_id,
                "display_name": member_names.get(user_id) or user_id,
                "points_total": int(points_map.get(user_id, 0)),
                "message_count": int(message_counts.get(user_id, 0)),
                "active_days": len(active_days.get(user_id, set())),
                "inactive_days": inactive_days,
                "social_score": float(social_stats.get(user_id, {}).get("score", 0.0)),
            }
        )
    rows = _score_metrics(metrics, config)
    social_hot = [
        {
            "user_id": user_id,
            "display_name": member_names.get(user_id) or user_id,
            **stats,
        }
        for user_id, stats in social_stats.items()
    ]
    social_hot.sort(key=lambda item: (-float(item["score"]), -int(item["received"]), str(item["user_id"])))
    snapshot = {
        "date_key": current.date().isoformat(),
        "conversation_id": int(trusted.conversation.id),
        "group_name": str(trusted.conversation.title or trusted.chatroom.name or "群聊"),
        "generated_at": current.isoformat(),
        "rows": rows,
        "social_hot": social_hot,
        "partners": partners,
        "bridges": bridges,
    }
    PluginStateStore(db, trusted.execution).put(
        SNAPSHOT_NAMESPACE,
        _snapshot_key(int(trusted.conversation.id), str(snapshot["date_key"])),
        snapshot,
        ttl_seconds=config.snapshot_retention_days * 24 * 60 * 60,
        now=current,
    )
    return snapshot


def _build_social_stats(
    directed_edges: Counter[tuple[str, str]],
    member_names: Mapping[str, str],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """从有向 @关系生成社交热度、无向搭子和连接广度三类统计。"""

    sent: Counter[str] = Counter()
    received: Counter[str] = Counter()
    partners_by_user: dict[str, set[str]] = defaultdict(set)
    pair_counts: Counter[tuple[str, str]] = Counter()
    for (sender_id, target_id), count in directed_edges.items():
        sent[sender_id] += count
        received[target_id] += count
        partners_by_user[sender_id].add(target_id)
        partners_by_user[target_id].add(sender_id)
        pair = tuple(sorted((sender_id, target_id)))
        pair_counts[pair] += count
    users = set(sent) | set(received) | set(partners_by_user)
    stats = {
        user_id: {
            "sent": int(sent[user_id]),
            "received": int(received[user_id]),
            "partner_count": len(partners_by_user[user_id]),
            "score": float(received[user_id] * 2 + sent[user_id] + len(partners_by_user[user_id]) * 2),
        }
        for user_id in users
    }
    partners = [
        {
            "member_ids": [user_a, user_b],
            "display_names": [member_names.get(user_a) or user_a, member_names.get(user_b) or user_b],
            "interaction_count": int(count),
            "score": float(count),
        }
        for (user_a, user_b), count in pair_counts.items()
    ]
    partners.sort(key=lambda item: (-int(item["interaction_count"]), item["member_ids"]))
    bridges = [
        {
            "user_id": user_id,
            "display_name": member_names.get(user_id) or user_id,
            "partner_count": len(partners_by_user[user_id]),
            "interaction_count": int(sent[user_id] + received[user_id]),
            "score": float(stats[user_id]["score"]),
        }
        for user_id in users
    ]
    bridges.sort(
        key=lambda item: (-int(item["partner_count"]), -float(item["score"]), str(item["user_id"]))
    )
    return stats, partners, bridges


def _score_metrics(metrics: Sequence[Mapping[str, Any]], config: ValueRankConfig) -> list[dict[str, Any]]:
    """按 95 分位缩放各指标，计算可解释分数并生成稳定排名。"""

    p95_points = _percentile95([float(item["points_total"]) for item in metrics])
    p95_messages = _percentile95([float(item["message_count"]) for item in metrics])
    p95_social = _percentile95([float(item["social_score"]) for item in metrics])
    scored: list[dict[str, Any]] = []
    for item in metrics:
        points_norm = _normalize_log(float(item["points_total"]), p95_points)
        message_norm = _normalize_linear(float(item["message_count"]), p95_messages)
        active_norm = _normalize_linear(float(item["active_days"]), float(config.active_window_days))
        social_norm = _normalize_linear(float(item["social_score"]), p95_social)
        penalty = config.inactivity_penalty_max * _normalize_linear(float(item["inactive_days"]), 30.0)
        score = max(
            0.0,
            round(
                config.base_score_scale
                * (
                    config.points_weight * points_norm
                    + config.message_weight * message_norm
                    + config.active_days_weight * active_norm
                    + config.social_weight * social_norm
                )
                - penalty,
                2,
            ),
        )
        scored.append(
            {
                **dict(item),
                "score": score,
                "score_detail": {
                    "points_norm": round(points_norm, 6),
                    "message_norm": round(message_norm, 6),
                    "active_norm": round(active_norm, 6),
                    "social_norm": round(social_norm, 6),
                    "penalty": round(penalty, 2),
                },
            }
        )
    scored.sort(
        key=lambda item: (
            -float(item["score"]),
            -int(item["message_count"]),
            -float(item["social_score"]),
            -int(item["points_total"]),
            str(item["user_id"]),
        )
    )
    total = len(scored)
    for rank, item in enumerate(scored, start=1):
        item["rank"] = rank
        item["title"] = _build_title(rank, total, int(item["inactive_days"]))
    return scored


def _build_weekly_report(
    db: Session,
    trusted: TrustedValueGroup,
    config: ValueRankConfig,
    *,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """从今日快照和七日前快照生成周报，不回扫原始消息。"""

    current_date = date.fromisoformat(str(snapshot["date_key"]))
    previous_key = (current_date - timedelta(days=7)).isoformat()
    previous = PluginStateStore(db, trusted.execution).get(
        SNAPSHOT_NAMESPACE,
        _snapshot_key(int(trusted.conversation.id), previous_key),
        now=now_shanghai(),
    )
    previous_scores = {
        str(row.get("user_id")): float(row.get("score") or 0)
        for row in (previous or {}).get("rows", [])
        if isinstance(row, Mapping)
    }
    changes = [
        {
            "user_id": str(row.get("user_id")),
            "display_name": str(row.get("display_name")),
            "delta": round(float(row.get("score") or 0) - previous_scores.get(str(row.get("user_id")), 0.0), 2),
        }
        for row in snapshot.get("rows", [])
        if isinstance(row, Mapping)
    ]
    changes.sort(key=lambda item: (-float(item["delta"]), str(item["user_id"])))
    return {
        "week_key": str(snapshot["date_key"]),
        "conversation_id": int(trusted.conversation.id),
        "group_name": str(snapshot.get("group_name") or "群聊"),
        "top_rows": list(snapshot.get("rows", []))[:5],
        "top_changes": changes[:5],
        "top_partners": list(snapshot.get("partners", []))[:3],
        "top_bridges": list(snapshot.get("bridges", []))[:3],
    }


def _format_my_value(
    snapshot: Mapping[str, Any],
    user_id: str,
    *,
    db: Session,
    trusted: TrustedValueGroup,
) -> str:
    """生成真实发送人的今日身价和昨日变化。"""

    row = next((item for item in snapshot.get("rows", []) if str(item.get("user_id")) == user_id), None)
    if not isinstance(row, Mapping):
        return ""
    previous_date = (date.fromisoformat(str(snapshot["date_key"])) - timedelta(days=1)).isoformat()
    previous = PluginStateStore(db, trusted.execution).get(
        SNAPSHOT_NAMESPACE,
        _snapshot_key(int(trusted.conversation.id), previous_date),
        now=now_shanghai(),
    )
    previous_row = next(
        (item for item in (previous or {}).get("rows", []) if str(item.get("user_id")) == user_id),
        None,
    )
    change = "新上榜"
    if isinstance(previous_row, Mapping):
        change = f"较昨日 {float(row.get('score') or 0) - float(previous_row.get('score') or 0):+.2f}"
    return "\n".join(
        [
            f"{row.get('display_name')} 的身价报告（{snapshot.get('date_key')}）",
            f"总身价：{float(row.get('score') or 0):.2f}",
            f"群内排名：第 {int(row.get('rank') or 0)} 名（{row.get('title')}）",
            f"积分资产：{int(row.get('points_total') or 0)}",
            f"统计窗口发言：{int(row.get('message_count') or 0)}",
            f"活跃天数：{int(row.get('active_days') or 0)}",
            f"社交得分：{float(row.get('social_score') or 0):.1f}",
            change,
        ]
    )


def _format_rankings(snapshot: Mapping[str, Any], limit: int) -> str:
    rows = list(snapshot.get("rows", []))[:limit]
    lines = [f"身价排行榜（Top{len(rows)} | {snapshot.get('date_key')}）"]
    for row in rows:
        lines.append(
            f"{int(row.get('rank') or 0)}. {row.get('display_name')} | "
            f"{float(row.get('score') or 0):.2f} | {row.get('title')}"
        )
    return "\n".join(lines)


def _format_social_hot(snapshot: Mapping[str, Any], limit: int) -> str:
    rows = list(snapshot.get("social_hot", []))[:limit]
    if not rows:
        return "近期暂无社交热度数据。"
    lines = [f"社交热度榜（Top{len(rows)}）"]
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. {row.get('display_name')} | 热度{float(row.get('score') or 0):.1f} | "
            f"被@{int(row.get('received') or 0)} | 主动@{int(row.get('sent') or 0)}"
        )
    return "\n".join(lines)


def _format_partners(snapshot: Mapping[str, Any], limit: int) -> str:
    rows = list(snapshot.get("partners", []))[:limit]
    if not rows:
        return "近期暂无搭子关系数据。"
    lines = [f"搭子榜（Top{len(rows)}）"]
    for index, row in enumerate(rows, start=1):
        names = row.get("display_names") or ["成员", "成员"]
        lines.append(f"{index}. {names[0]} × {names[1]} | 互动{int(row.get('interaction_count') or 0)}")
    return "\n".join(lines)


def _format_bridges(snapshot: Mapping[str, Any], limit: int) -> str:
    rows = list(snapshot.get("bridges", []))[:limit]
    if not rows:
        return "近期暂无社交桥梁数据。"
    lines = [f"社交桥梁榜（Top{len(rows)}）"]
    for index, row in enumerate(rows, start=1):
        lines.append(
            f"{index}. {row.get('display_name')} | 连接{int(row.get('partner_count') or 0)}人 | "
            f"互动{int(row.get('interaction_count') or 0)}"
        )
    return "\n".join(lines)


def _format_social_graph(snapshot: Mapping[str, Any], limit: int) -> str:
    """用桥梁人物和搭子关系生成渠道无关社交图摘要。"""

    bridges = list(snapshot.get("bridges", []))[:limit]
    partners = list(snapshot.get("partners", []))[:limit]
    if not bridges and not partners:
        return "近期社交关系数据不足。"
    lines = ["群友社交关系摘要"]
    if bridges:
        lines.append("社交桥梁")
        for row in bridges:
            lines.append(f"- {row.get('display_name')}：连接{int(row.get('partner_count') or 0)}人")
    if partners:
        lines.append("核心搭子")
        for row in partners:
            names = row.get("display_names") or ["成员", "成员"]
            lines.append(f"- {names[0]} × {names[1]}：互动{int(row.get('interaction_count') or 0)}")
    return "\n".join(lines)


def _format_trend(db: Session, trusted: TrustedValueGroup, user_id: str, days: int) -> str:
    """读取当前绑定有限历史快照，生成真实发送人的趋势。"""

    cutoff = now_shanghai().date() - timedelta(days=days - 1)
    prefix = f"conversation-{int(trusted.conversation.id)}:date-"
    records = db.scalars(
        select(PluginStateRecord).where(
            PluginStateRecord.bot_plugin_id == int(trusted.binding.id),
            PluginStateRecord.namespace == SNAPSHOT_NAMESPACE,
            PluginStateRecord.state_key.like(f"{prefix}%"),
        )
    ).all()
    points: list[tuple[str, float, int, str]] = []
    for record in records:
        payload = parse_state_payload(record.payload_json)
        try:
            stat_date = date.fromisoformat(str(payload.get("date_key") or ""))
        except ValueError:
            continue
        if stat_date < cutoff:
            continue
        row = next((item for item in payload.get("rows", []) if str(item.get("user_id")) == user_id), None)
        if isinstance(row, Mapping):
            points.append(
                (
                    stat_date.isoformat(),
                    float(row.get("score") or 0),
                    int(row.get("rank") or 0),
                    str(row.get("display_name") or user_id),
                )
            )
    points.sort(key=lambda item: item[0])
    if not points:
        return ""
    delta = points[-1][1] - points[0][1]
    lines = [f"{points[-1][3]} 的身价趋势（近{days}天）"]
    lines.extend(f"- {item[0]}：{item[1]:.2f}（第{item[2]}名）" for item in points)
    lines.append(f"趋势结论：{'上涨' if delta > 0 else '下滑' if delta < 0 else '持平'} {delta:+.2f}")
    return "\n".join(lines)


def _format_weekly_report(report: Mapping[str, Any]) -> str:
    lines = [f"身价周报（{report.get('week_key')}）", f"群聊：{report.get('group_name')}", "", "本周身价榜"]
    for row in report.get("top_rows", []):
        lines.append(f"{int(row.get('rank') or 0)}. {row.get('display_name')}：{float(row.get('score') or 0):.2f}")
    changes = [item for item in report.get("top_changes", []) if float(item.get("delta") or 0) > 0]
    if changes:
        lines.extend(["", "本周涨幅"])
        for item in changes[:3]:
            lines.append(f"- {item.get('display_name')}：{float(item.get('delta') or 0):+.2f}")
    if report.get("top_partners"):
        lines.extend(["", "本周搭子"])
        for item in report["top_partners"]:
            names = item.get("display_names") or ["成员", "成员"]
            lines.append(f"- {names[0]} × {names[1]}：互动{int(item.get('interaction_count') or 0)}")
    return "\n".join(lines)


def _format_explanation(config: ValueRankConfig) -> str:
    return (
        "身价算法说明\n"
        f"- 积分权重：{config.points_weight:.2f}\n"
        f"- 发言权重：{config.message_weight:.2f}（{config.message_window_days}天）\n"
        f"- 活跃权重：{config.active_days_weight:.2f}（{config.active_window_days}天）\n"
        f"- 社交权重：{config.social_weight:.2f}（{config.social_window_days}天）\n"
        f"- 潜水惩罚上限：{config.inactivity_penalty_max:.0f}\n"
        "各指标按群内 95 分位缩放，减少极端值对刷榜结果的影响。"
    )


def _load_config(raw: Mapping[str, Any]) -> ValueRankConfig:
    """读取并归一化权重，限制窗口和结果规模。"""

    raw_weights = [
        _bounded_float(raw.get("points_weight"), 0.30, 0.0, 1.0),
        _bounded_float(raw.get("message_weight"), 0.35, 0.0, 1.0),
        _bounded_float(raw.get("active_days_weight"), 0.20, 0.0, 1.0),
        _bounded_float(raw.get("social_weight"), 0.15, 0.0, 1.0),
    ]
    total = sum(raw_weights) or 1.0
    normalized = [weight / total for weight in raw_weights]
    administrators = frozenset(
        str(item).strip()
        for item in raw.get("administrator_user_ids", [])
        if str(item).strip()
    ) if isinstance(raw.get("administrator_user_ids"), (list, tuple)) else frozenset()
    return ValueRankConfig(
        message_window_days=_bounded_int(raw.get("message_window_days"), 7, 1, 90),
        active_window_days=_bounded_int(raw.get("active_window_days"), 30, 1, 365),
        social_window_days=_bounded_int(raw.get("social_window_days"), 7, 1, 90),
        points_weight=normalized[0],
        message_weight=normalized[1],
        active_days_weight=normalized[2],
        social_weight=normalized[3],
        inactivity_penalty_max=_bounded_float(raw.get("inactivity_penalty_max"), 150.0, 0.0, 10000.0),
        base_score_scale=_bounded_float(raw.get("base_score_scale"), 1000.0, 1.0, 1_000_000.0),
        default_rank_limit=_bounded_int(raw.get("default_rank_limit"), 10, 1, 50),
        max_rank_limit=_bounded_int(raw.get("max_rank_limit"), 50, 1, 100),
        default_trend_days=_bounded_int(raw.get("default_trend_days"), 7, 1, 30),
        max_trend_days=_bounded_int(raw.get("max_trend_days"), 30, 1, 180),
        snapshot_retention_days=_bounded_int(raw.get("snapshot_retention_days"), 180, 7, 365),
        weekly_report_retention_days=_bounded_int(raw.get("weekly_report_retention_days"), 14, 1, 90),
        administrator_user_ids=administrators,
    )


def _parse_command(content: str) -> tuple[str, str]:
    """按最长命令前缀解析参数，不保留旧帮助别名。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    for command in sorted(COMMANDS, key=len, reverse=True):
        if normalized == command:
            return command, ""
        if normalized.startswith(f"{command} "):
            return command, normalized[len(command) :].strip()
    return "", ""


def _parse_bounded_argument(value: str, default: int, maximum: int) -> int:
    try:
        normalized = int(str(value or "").strip())
    except (TypeError, ValueError):
        normalized = default
    return max(1, min(normalized, maximum))


def _snapshot_key(conversation_id: int, date_key: str) -> str:
    return f"conversation-{int(conversation_id)}:date-{date_key}"


def _weekly_report_key(conversation_id: int, week_key: str) -> str:
    return f"conversation-{int(conversation_id)}:week-{week_key}"


def _percentile95(values: Sequence[float]) -> float:
    normalized = sorted(max(0.0, float(value)) for value in values)
    if not normalized:
        return 1.0
    index = max(0, math.ceil(len(normalized) * 0.95) - 1)
    return max(normalized[index], 1.0)


def _normalize_linear(value: float, upper: float) -> float:
    return max(0.0, min(float(value) / max(float(upper), 1.0), 1.0))


def _normalize_log(value: float, upper: float) -> float:
    return max(0.0, min(math.log1p(max(value, 0.0)) / math.log1p(max(upper, 1.0)), 1.0))


def _build_title(rank: int, total: int, inactive_days: int) -> str:
    if inactive_days >= 30:
        return "深海潜水员"
    ratio = rank / max(total, 1)
    if ratio <= 0.1:
        return "群聊顶流"
    if ratio <= 0.3:
        return "核心成员"
    if ratio <= 0.6:
        return "稳定活跃"
    return "成长成员"


def _text_result(
    command: str,
    text: str,
    *,
    provider_status: str = "completed",
    snapshot_member_count: int,
) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text,
        output={
            "provider_status": provider_status,
            "command": command,
            "snapshot_member_count": snapshot_member_count,
            "reply_text": text,
            "replies": standard_replies([text_reply(text)]),
        },
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status, "replies": standard_replies([text_reply(message)])},
    )


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
