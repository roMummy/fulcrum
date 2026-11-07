from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Chatroom, ChatroomMember, Conversation
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply


@dataclass(frozen=True)
class InactiveRankConfig:
    """潜水排行参数上限，防止群命令触发无界查询或超长回复。"""

    default_days: int
    default_limit: int
    max_days: int
    max_limit: int


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通动作只提供运行状态；排行必须使用带数据库会话的可信入口。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="潜水排行插件已就绪。",
            output={
                "default_days": config.default_days,
                "default_limit": config.default_limit,
                "max_days": config.max_days,
                "max_limit": config.max_limit,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("潜水排行只能通过真实群聊会话查询。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """只在当前租户、机器人、渠道和群会话内统计成员活跃时间。"""

    if request.action != "execute":
        return execute_action(request)
    context, conversation, error = _require_group_context(db, request.context)
    if error is not None or context is None or conversation is None:
        return error or _error_result("潜水排行缺少可信群聊上下文。", "trusted_context_required")

    config = _load_config(request.config)
    parameters = _parse_parameters(request.payload, config)
    if parameters is None:
        return _error_result(
            f"命令格式：潜水排行 [1-{config.max_days}天] [1-{config.max_limit}名]",
            "invalid_parameters",
        )
    days, limit = parameters
    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(context.channel_id or 0),
            Chatroom.external_id == str(conversation.external_id),
        )
    )
    if chatroom is None:
        return _error_result("当前群还没有成员数据。", "chatroom_not_found")

    current = now_shanghai()
    threshold = current - timedelta(days=days)
    members = db.scalars(
        select(ChatroomMember).where(
            ChatroomMember.tenant_id == int(context.tenant_id),
            ChatroomMember.bot_id == int(context.bot_id),
            ChatroomMember.channel_id == int(context.channel_id or 0),
            ChatroomMember.chatroom_id == int(chatroom.id),
            ChatroomMember.status != "left",
        )
    ).all()

    # 成员表已经由渠道同步和入站消息更新 last_active_at。插件只消费这份可信数据，
    # 不扫描跨群消息，也不根据 payload 中的群号重新定位其他会话。
    ranking_rows: list[dict[str, Any]] = []
    for member in members:
        member_id = str(member.member_external_id or "").strip()
        if not member_id:
            continue
        last_active = member.last_active_at
        normalized_last_active = ensure_shanghai_datetime(last_active) if last_active is not None else None
        if normalized_last_active is not None and normalized_last_active >= threshold:
            continue
        inactivity_days = (
            config.max_days + 1
            if normalized_last_active is None
            else max(int((current - normalized_last_active).total_seconds() // 86_400), 0)
        )
        ranking_rows.append(
            {
                "name": str(member.display_name or member.nickname or member_id).strip() or member_id,
                "inactivity_days": inactivity_days,
                "never_spoken": normalized_last_active is None,
            }
        )

    ranking_rows.sort(
        key=lambda item: (
            not bool(item["never_spoken"]),
            -int(item["inactivity_days"]),
            str(item["name"]),
        )
    )
    selected = ranking_rows[:limit]
    if not selected:
        reply_text = f"近 {days} 天没有符合条件的潜水成员。"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="当前群没有符合条件的潜水成员。",
            output={
                "provider_status": "empty",
                "days": days,
                "limit": limit,
                "rank_count": 0,
                "reply_text": reply_text,
                "replies": standard_replies([text_reply(reply_text)]),
            },
        )

    lines = [f"潜水排行（近 {days} 天未发言，前 {limit} 名）"]
    for index, item in enumerate(selected, start=1):
        status = "从未发言" if item["never_spoken"] else f"{item['inactivity_days']} 天未发言"
        lines.append(f"{index}. {item['name']} - {status}")
    reply_text = "\n".join(lines)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="潜水排行已生成。",
        output={
            "provider_status": "ranking",
            "days": days,
            "limit": limit,
            "rank_count": len(selected),
            # 执行记录不保存成员主键和完整排行对象，只保留最终业务文本。
            "reply_text": reply_text,
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def _require_group_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[PluginExecutionContext | None, Conversation | None, PluginExecutionResult | None]:
    """验证平台构造的会话主键，不接受业务载荷中的群号或渠道号。"""

    if (
        context is None
        or context.conversation_id is None
        or context.channel_id is None
        or context.chat_type != "group"
    ):
        return None, None, _error_result("请在群聊中使用潜水排行。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
        or conversation.chat_type != "group"
    ):
        return None, None, _error_result("当前群聊上下文无效。", "trusted_context_required")
    return context, conversation, None


def _parse_parameters(
    payload: Mapping[str, Any],
    config: InactiveRankConfig,
) -> tuple[int, int] | None:
    """只解析当前中文命令的两个位置参数，删除旧英文命令和多套字段入口。"""

    argument_text = str(payload.get("command_args") or "").strip()
    if not argument_text:
        content = str(payload.get("content") or "").strip()
        prefix, _, suffix = content.partition(" ")
        argument_text = suffix.strip() if prefix == "潜水排行" else ""
    parts = [item for item in argument_text.split() if item]
    if len(parts) > 2:
        return None
    try:
        days = int(parts[0]) if parts else config.default_days
        limit = int(parts[1]) if len(parts) == 2 else config.default_limit
    except ValueError:
        return None
    if days <= 0 or limit <= 0:
        return None
    return min(days, config.max_days), min(limit, config.max_limit)


def _load_config(raw: Mapping[str, Any]) -> InactiveRankConfig:
    max_days = max(1, min(_coerce_int(raw.get("max_days"), 365), 3650))
    max_limit = max(1, min(_coerce_int(raw.get("max_limit"), 50), 100))
    return InactiveRankConfig(
        default_days=max(1, min(_coerce_int(raw.get("default_days"), 60), max_days)),
        default_limit=max(1, min(_coerce_int(raw.get("default_limit"), 10), max_limit)),
        max_days=max_days,
        max_limit=max_limit,
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
