from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom, Conversation, Message
from app.services.outbound_batch import enqueue_outbound_jobs_by_conversation
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply


FORWARD_PREFIX = "[群桥]"
MANAGEMENT_COMMANDS = {
    "创建跨群",
    "添加群",
    "查看跨群列表",
    "查看跨群",
    "退出跨群",
    "解散跨群",
}


@dataclass(frozen=True)
class GroupVirtualConfig:
    """群桥配置只保存平台群主键，不保存渠道外部群号。"""

    admin_user_ids: tuple[str, ...]
    bridges: dict[str, tuple[int, ...]]
    prefix_template: str


def matches_event(context) -> bool | dict[str, object]:
    """群桥观察全部入站群文本，已带群桥前缀的回显直接排除。"""

    content = str(getattr(context, "content", "") or "").strip()
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "").strip().lower()
    if chat_type != "group" or message_type != "text" or not content:
        return {"matched": False, "reason": "group_text_required"}
    if content.startswith(FORWARD_PREFIX):
        return {"matched": False, "reason": "forward_echo_ignored"}
    return {"matched": True, "reason": "group_bridge_candidate"}


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只处理重载，群桥管理和转发必须使用可信数据库入口。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="跨群群桥插件已就绪。",
            output={
                "bridge_count": len(config.bridges),
                "admin_count": len(config.admin_user_ids),
                "prefix_template": config.prefix_template,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("群桥只能通过真实群聊消息执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """处理当前群管理命令，或把可信入站文本排入其他群的出站队列。"""

    if request.action != "execute":
        return execute_action(request)
    context, conversation, chatroom, message, error = _require_group_message_context(db, request.context)
    if error is not None or context is None or conversation is None or chatroom is None or message is None:
        return error or _error_result("群桥缺少可信群消息上下文。", "trusted_context_required")

    config = _load_config(request.config)
    command_name = str(request.payload.get("command_name") or "").strip()
    if command_name in MANAGEMENT_COMMANDS:
        return _handle_management_command(db, request, context, chatroom, config, command_name)
    if str(message.content or "").strip().startswith(FORWARD_PREFIX):
        return _ignored("已转发消息不再重复处理。", "forward_echo_ignored")
    return _forward_group_message(db, context, conversation, chatroom, message, config)


def _handle_management_command(
    db: Session,
    request: PluginExecutionRequest,
    context: PluginExecutionContext,
    current_chatroom: Chatroom,
    config: GroupVirtualConfig,
    command_name: str,
) -> PluginExecutionResult:
    """管理员仅操作当前群，不允许在命令正文中指定其他群主键。"""

    sender_id = str(context.sender_id or "").strip()
    if not config.admin_user_ids:
        return _error_result("请先在插件配置中设置管理员用户 ID。", "admin_not_configured")
    if sender_id not in config.admin_user_ids:
        return _error_result("只有插件管理员可以维护群桥。", "forbidden")

    arguments = str(request.payload.get("command_args") or "").strip()
    if command_name == "查看跨群列表":
        return _list_bridges(db, context, config.bridges)
    bridge_name = _normalize_bridge_name(arguments)
    if not bridge_name:
        return _error_result(f"命令格式：#{command_name} 群桥名称", "invalid_parameters")
    bridges = {name: list(group_ids) for name, group_ids in config.bridges.items()}

    if command_name == "创建跨群":
        if bridge_name in bridges:
            return _error_result(f"群桥“{bridge_name}”已经存在。", "bridge_exists")
        _remove_chatroom_from_other_bridges(bridges, int(current_chatroom.id))
        bridges[bridge_name] = [int(current_chatroom.id)]
        return _config_result(bridges, f"已创建群桥“{bridge_name}”，当前群已加入。")
    if command_name == "添加群":
        if bridge_name not in bridges:
            return _error_result(f"群桥“{bridge_name}”不存在。", "bridge_missing")
        _remove_chatroom_from_other_bridges(bridges, int(current_chatroom.id), keep_bridge=bridge_name)
        if int(current_chatroom.id) not in bridges[bridge_name]:
            bridges[bridge_name].append(int(current_chatroom.id))
        return _config_result(bridges, f"当前群已加入群桥“{bridge_name}”。")
    if command_name == "退出跨群":
        group_ids = bridges.get(bridge_name)
        if not group_ids or int(current_chatroom.id) not in group_ids:
            return _error_result(f"当前群不在群桥“{bridge_name}”中。", "bridge_membership_missing")
        group_ids.remove(int(current_chatroom.id))
        if not group_ids:
            bridges.pop(bridge_name, None)
        return _config_result(bridges, f"当前群已退出群桥“{bridge_name}”。")
    if command_name == "解散跨群":
        if bridge_name not in bridges:
            return _error_result(f"群桥“{bridge_name}”不存在。", "bridge_missing")
        bridges.pop(bridge_name, None)
        return _config_result(bridges, f"群桥“{bridge_name}”已解散。")
    if command_name == "查看跨群":
        group_ids = config.bridges.get(bridge_name)
        if not group_ids:
            return _error_result(f"群桥“{bridge_name}”不存在。", "bridge_missing")
        return _bridge_detail(db, context, bridge_name, group_ids)
    return _ignored("当前消息不是群桥管理命令。", "ignored")


def _forward_group_message(
    db: Session,
    context: PluginExecutionContext,
    source_conversation: Conversation,
    source_chatroom: Chatroom,
    message: Message,
    config: GroupVirtualConfig,
) -> PluginExecutionResult:
    """把当前入站文本排入同一群桥其他群的标准出站任务。"""

    bridge_name, target_ids = _resolve_bridge_targets(config.bridges, int(source_chatroom.id))
    if not target_ids:
        return _ignored("当前群没有可转发的群桥目标。", "bridge_target_missing")
    target_conversations = _load_target_conversations(db, context, target_ids)
    if not target_conversations:
        return _ignored("群桥目标暂时没有可用会话。", "target_conversation_missing")

    sender_name = str(message.sender_name or context.sender_id or "群成员").strip() or "群成员"
    source_name = str(source_chatroom.remark or source_chatroom.name or source_conversation.title or "当前群").strip()
    forward_text = _build_forward_text(config.prefix_template, sender_name, source_name, str(message.content or ""))
    batch = enqueue_outbound_jobs_by_conversation(
        db,
        conversations=target_conversations,
        plugin_code="group_virtual",
        related_type="message",
        related_id=int(message.id),
        priority=95,
        payload_builder=lambda conversation, _channel: [
            {
                "message_type": "text",
                "content": forward_text,
                "external_id": str(conversation.external_id),
                "receiver_id": str(conversation.external_id),
                "chat_type": "group",
            }
        ],
        log_context={"bridge_name": bridge_name, "source_chatroom_id": int(source_chatroom.id)},
    )
    job_ids = list(batch.get("job_ids") or [])
    if not job_ids:
        return _error_result("群桥消息暂未转发，请稍后重试。", "forward_failed")
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"群桥消息已进入 {len(job_ids)} 个目标队列。",
        output={
            "provider_status": "forward_queued",
            "bridge_name": bridge_name,
            "target_count": len(target_conversations),
            "queued_count": len(job_ids),
        },
    )


def _require_group_message_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[
    PluginExecutionContext | None,
    Conversation | None,
    Chatroom | None,
    Message | None,
    PluginExecutionResult | None,
]:
    """验证当前消息、会话和群记录属于同一可信租户与机器人。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
        or context.message_type != "text"
    ):
        return None, None, None, None, _error_result("群桥只处理真实入站群文本。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    if (
        conversation is None
        or message is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
        or conversation.chat_type != "group"
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or message.direction != "in"
        or message.message_type != "text"
    ):
        return None, None, None, None, _error_result("当前消息不是可信入站群文本。", "trusted_context_required")
    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(context.channel_id),
            Chatroom.external_id == str(conversation.external_id),
            Chatroom.is_active.is_(True),
        )
    )
    if chatroom is None:
        return None, None, None, None, _error_result("当前群尚未启用管理。", "chatroom_not_found")
    return context, conversation, chatroom, message, None


def _load_target_conversations(
    db: Session,
    context: PluginExecutionContext,
    chatroom_ids: tuple[int, ...],
) -> list[Conversation]:
    """把群桥内部 ID 转换为当前机器人可用的跨渠道群会话。"""

    chatrooms = db.scalars(
        select(Chatroom).where(
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.id.in_(chatroom_ids),
            Chatroom.is_active.is_(True),
        )
    ).all()
    conversations: list[Conversation] = []
    for chatroom in chatrooms:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.tenant_id == int(context.tenant_id),
                Conversation.bot_id == int(context.bot_id),
                Conversation.channel_id == int(chatroom.channel_id),
                Conversation.external_id == str(chatroom.external_id),
                Conversation.chat_type == "group",
            )
        )
        channel = db.get(Channel, int(chatroom.channel_id))
        if conversation is not None and channel is not None and channel.is_enabled:
            conversations.append(conversation)
    return conversations


def _resolve_bridge_targets(
    bridges: Mapping[str, tuple[int, ...]],
    source_chatroom_id: int,
) -> tuple[str, tuple[int, ...]]:
    for bridge_name, chatroom_ids in bridges.items():
        if source_chatroom_id in chatroom_ids:
            return bridge_name, tuple(item for item in chatroom_ids if item != source_chatroom_id)
    return "", ()


def _remove_chatroom_from_other_bridges(
    bridges: dict[str, list[int]],
    chatroom_id: int,
    *,
    keep_bridge: str | None = None,
) -> None:
    """一个群只属于一个群桥，避免同一消息被多套桥重复转发。"""

    empty_names: list[str] = []
    for bridge_name, group_ids in bridges.items():
        if bridge_name == keep_bridge:
            continue
        bridges[bridge_name] = [item for item in group_ids if item != chatroom_id]
        if not bridges[bridge_name]:
            empty_names.append(bridge_name)
    for bridge_name in empty_names:
        bridges.pop(bridge_name, None)


def _list_bridges(
    db: Session,
    context: PluginExecutionContext,
    bridges: Mapping[str, tuple[int, ...]],
) -> PluginExecutionResult:
    if not bridges:
        return _text_result("当前还没有群桥。", "empty")
    lines = ["群桥列表："]
    for bridge_name, group_ids in sorted(bridges.items()):
        valid_count = len(_load_chatrooms(db, context, group_ids))
        lines.append(f"{bridge_name}：{valid_count} 个群")
    return _text_result("\n".join(lines), "list")


def _bridge_detail(
    db: Session,
    context: PluginExecutionContext,
    bridge_name: str,
    group_ids: tuple[int, ...],
) -> PluginExecutionResult:
    chatrooms = _load_chatrooms(db, context, group_ids)
    lines = [f"群桥“{bridge_name}”："]
    lines.extend(f"- {str(item.remark or item.name or item.external_id).strip()}" for item in chatrooms)
    return _text_result("\n".join(lines), "bridge_found")


def _load_chatrooms(
    db: Session,
    context: PluginExecutionContext,
    group_ids: tuple[int, ...],
) -> list[Chatroom]:
    if not group_ids:
        return []
    return list(
        db.scalars(
            select(Chatroom).where(
                Chatroom.tenant_id == int(context.tenant_id),
                Chatroom.bot_id == int(context.bot_id),
                Chatroom.id.in_(group_ids),
                Chatroom.is_active.is_(True),
            )
        ).all()
    )


def _config_result(bridges: Mapping[str, list[int]], message: str) -> PluginExecutionResult:
    """群桥变更通过绑定配置补丁保存，不由插件直接写 BotPlugin。"""

    normalized = {name: list(dict.fromkeys(group_ids)) for name, group_ids in bridges.items() if group_ids}
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": "config_updated",
            "config_patch": {"bridges": normalized},
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _build_forward_text(template: str, sender_name: str, source_name: str, content: str) -> str:
    """模板只替换两个业务变量，未知占位符保持原样且不能执行格式表达式。"""

    prefix = str(template or "").replace("{sender}", sender_name).replace("{group}", source_name)
    if not prefix.startswith(FORWARD_PREFIX):
        prefix = f"{FORWARD_PREFIX} {prefix}"
    return f"{prefix}{str(content or '').strip()}"


def _load_config(raw: Mapping[str, Any]) -> GroupVirtualConfig:
    """只接受当前平台群主键列表，旧外部群号桥接配置不会继续生效。"""

    bridges: dict[str, tuple[int, ...]] = {}
    raw_bridges = raw.get("bridges")
    if isinstance(raw_bridges, Mapping):
        claimed_chatrooms: set[int] = set()
        for name, values in raw_bridges.items():
            bridge_name = _normalize_bridge_name(str(name or ""))
            if not bridge_name or not isinstance(values, (list, tuple)):
                continue
            group_ids: list[int] = []
            for value in values:
                try:
                    chatroom_id = int(value)
                except (TypeError, ValueError):
                    continue
                if chatroom_id <= 0 or chatroom_id in claimed_chatrooms:
                    continue
                claimed_chatrooms.add(chatroom_id)
                group_ids.append(chatroom_id)
            if group_ids:
                bridges[bridge_name] = tuple(group_ids)
    raw_admins = raw.get("admin_user_ids")
    admins = tuple(
        dict.fromkeys(str(item or "").strip() for item in raw_admins if str(item or "").strip())
    ) if isinstance(raw_admins, (list, tuple)) else ()
    prefix_template = str(raw.get("prefix_template") or f"{FORWARD_PREFIX} {{sender}} 来自 {{group}}：").strip()
    return GroupVirtualConfig(
        admin_user_ids=admins,
        bridges=bridges,
        prefix_template=prefix_template or f"{FORWARD_PREFIX} {{sender}} 来自 {{group}}：",
    )


def _normalize_bridge_name(value: str) -> str:
    return " ".join(str(value or "").strip().split())[:50]


def _text_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _ignored(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={"provider_status": provider_status},
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
