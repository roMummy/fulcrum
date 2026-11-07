from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.entities import Chatroom, Conversation
from app.services.channel_group_operations import invite_group_members
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply


CONFIG_COMMAND = "加群配置"
JOIN_COMMAND = "加群"


@dataclass(frozen=True)
class InviteConfig:
    """自动拉群只保留当前产品需要的管理员、私聊限制和单群映射。"""

    admin_user_ids: tuple[str, ...]
    mappings: dict[str, int]
    require_direct_chat: bool
    response_prefix: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只处理重载；真实邀请必须通过可信数据库入口执行。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="自动拉群插件已就绪。",
            output={
                "mapping_count": len(config.mappings),
                "admin_count": len(config.admin_user_ids),
                "require_direct_chat": config.require_direct_chat,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("自动拉群只能通过真实会话执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """根据平台解析出的当前中文命令维护映射或邀请当前私聊用户。"""

    if request.action != "execute":
        return execute_action(request)
    context, conversation, error = _require_context(db, request.context)
    if error is not None or context is None or conversation is None:
        return error or _error_result("自动拉群缺少可信会话上下文。", "trusted_context_required")
    command_name = str(request.payload.get("command_name") or "").strip()
    if command_name == CONFIG_COMMAND:
        return _handle_config_command(db, request, context, _load_config(request.config))
    if command_name == JOIN_COMMAND:
        return _handle_join_command(db, request, context, conversation, _load_config(request.config))
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="当前消息不是自动拉群命令。",
        output={"provider_status": "ignored"},
    )


def _handle_config_command(
    db: Session,
    request: PluginExecutionRequest,
    context: PluginExecutionContext,
    config: InviteConfig,
) -> PluginExecutionResult:
    """管理员通过中文命令维护关键词与平台群记录的一对一映射。"""

    sender_id = str(context.sender_id or "").strip()
    if not config.admin_user_ids:
        return _error_result("请先在插件配置中设置管理员用户 ID。", "admin_not_configured")
    if sender_id not in config.admin_user_ids:
        return _error_result("只有插件管理员可以维护加群关键词。", "forbidden")

    arguments = str(request.payload.get("command_args") or "").strip()
    action, _, body = arguments.partition(" ")
    action = action.strip()
    body = body.strip()
    if not action or action == "帮助":
        return _text_result(
            "配置命令：\n"
            "#加群配置 列表\n"
            "#加群配置 查看 关键词\n"
            "#加群配置 添加 关键词 群名称\n"
            "#加群配置 删除 关键词",
            "help",
        )
    if action == "列表":
        return _list_result(db, context, config.mappings)
    if action == "查看":
        key = _normalize_keyword(body)
        if not key:
            return _error_result("请输入要查看的关键词。", "invalid_parameters")
        chatroom_id = config.mappings.get(key)
        if chatroom_id is None:
            return _text_result(f"关键词“{key}”还没有绑定群聊。", "mapping_missing")
        chatroom = _load_mapped_chatroom(db, context, chatroom_id)
        if chatroom is None:
            return _text_result(f"关键词“{key}”绑定的群已失效，请重新配置。", "mapping_stale")
        return _text_result(f"{key} -> {_chatroom_label(chatroom)}", "mapping_found")
    if action == "删除":
        key = _normalize_keyword(body)
        if not key:
            return _error_result("请输入要删除的关键词。", "invalid_parameters")
        updated = dict(config.mappings)
        updated.pop(key, None)
        return _config_result(updated, f"已删除关键词“{key}”。")
    if action == "添加":
        key_text, separator, target_text = body.partition(" ")
        key = _normalize_keyword(key_text)
        target = target_text.strip() if separator else ""
        if not key or not target:
            return _error_result("命令格式：#加群配置 添加 关键词 群名称", "invalid_parameters")
        chatroom, resolve_error = _resolve_chatroom(db, context, target)
        if resolve_error:
            return _error_result(resolve_error, "chatroom_not_found")
        updated = dict(config.mappings)
        updated[key] = int(chatroom.id)
        return _config_result(updated, f"已将“{key}”绑定到“{_chatroom_label(chatroom)}”。")
    return _error_result("不支持的配置动作，请发送 #加群配置 帮助。", "invalid_action")


def _handle_join_command(
    db: Session,
    request: PluginExecutionRequest,
    context: PluginExecutionContext,
    conversation: Conversation,
    config: InviteConfig,
) -> PluginExecutionResult:
    """邀请当前私聊用户加入关键词对应的当前渠道群。"""

    if config.require_direct_chat and conversation.chat_type != "direct":
        return _text_result("请私聊机器人发送 #加群 关键词。", "direct_chat_required")
    sender_id = str(context.sender_id or "").strip()
    if not sender_id:
        return _error_result("当前渠道没有提供用户标识，无法发起邀请。", "sender_required")
    key = _normalize_keyword(str(request.payload.get("command_args") or ""))
    if not key:
        return _error_result("命令格式：#加群 关键词", "invalid_parameters")
    chatroom_id = config.mappings.get(key)
    if chatroom_id is None:
        return _text_result(f"暂未开放“{key}”对应的群聊。", "mapping_missing")
    chatroom = _load_mapped_chatroom(db, context, chatroom_id)
    if chatroom is None:
        return _error_result("目标群已失效，请联系管理员重新配置。", "mapping_stale")

    result = invite_group_members(
        db,
        context=context,
        chatroom_id=int(chatroom.id),
        member_external_ids=[sender_id],
    )
    if not bool(result.get("ok")):
        # 渠道错误详情会进入平台执行记录，最终用户只看到稳定的业务提示，避免
        # 暴露协议地址、上游响应或内部错误码。
        return _error_result("群邀请暂未发送，请稍后重试或联系管理员。", "invite_failed")
    prefix = f"{config.response_prefix} " if config.response_prefix else ""
    return _text_result(f"{prefix}群邀请已发送，请在客户端确认。", "invite_sent")


def _require_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[PluginExecutionContext | None, Conversation | None, PluginExecutionResult | None]:
    """验证平台注入的租户、机器人、渠道和会话主键。"""

    if context is None or context.channel_id is None or context.conversation_id is None:
        return None, None, _error_result("自动拉群缺少可信会话。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
    ):
        return None, None, _error_result("当前会话不属于该机器人渠道。", "trusted_context_required")
    return context, conversation, None


def _resolve_chatroom(
    db: Session,
    context: PluginExecutionContext,
    target: str,
) -> tuple[Chatroom | None, str | None]:
    """按当前渠道内的群名称、备注或外部 ID 精确查找目标群。"""

    normalized = str(target or "").strip()
    rows = db.scalars(
        select(Chatroom).where(
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(context.channel_id or 0),
            Chatroom.is_active.is_(True),
            or_(
                Chatroom.name == normalized,
                Chatroom.remark == normalized,
                Chatroom.external_id == normalized,
            ),
        )
    ).all()
    if not rows:
        return None, f"当前渠道没有找到已启用群“{normalized}”。"
    if len(rows) > 1:
        return None, f"存在多个同名群“{normalized}”，请使用群备注或完整群标识。"
    return rows[0], None


def _load_mapped_chatroom(
    db: Session,
    context: PluginExecutionContext,
    chatroom_id: int,
) -> Chatroom | None:
    """加载配置中的内部群 ID，并再次限制在当前可信渠道。"""

    return db.scalar(
        select(Chatroom).where(
            Chatroom.id == int(chatroom_id),
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(context.channel_id or 0),
            Chatroom.is_active.is_(True),
        )
    )


def _list_result(
    db: Session,
    context: PluginExecutionContext,
    mappings: Mapping[str, int],
) -> PluginExecutionResult:
    """列表只展示仍属于当前渠道的有效群，失效映射不泄露内部资源。"""

    lines: list[str] = []
    for key, chatroom_id in sorted(mappings.items()):
        chatroom = _load_mapped_chatroom(db, context, chatroom_id)
        if chatroom is not None:
            lines.append(f"{key} -> {_chatroom_label(chatroom)}")
    if not lines:
        return _text_result("当前还没有配置加群关键词。", "empty")
    return _text_result("加群关键词：\n" + "\n".join(lines), "list")


def _config_result(mappings: Mapping[str, int], message: str) -> PluginExecutionResult:
    """通过运行时配置补丁保存映射，不让插件直接修改绑定表。"""

    reply = text_reply(message)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": "config_updated",
            "config_patch": {"mappings": dict(mappings)},
            "replies": standard_replies([reply]),
        },
    )


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


def _load_config(raw: Mapping[str, Any]) -> InviteConfig:
    """只接受当前整数群 ID 映射，旧外部群号配置不会继续生效。"""

    mappings: dict[str, int] = {}
    raw_mappings = raw.get("mappings")
    if isinstance(raw_mappings, Mapping):
        for key, value in raw_mappings.items():
            normalized_key = _normalize_keyword(str(key or ""))
            try:
                chatroom_id = int(value)
            except (TypeError, ValueError):
                continue
            if normalized_key and chatroom_id > 0:
                mappings[normalized_key] = chatroom_id
    raw_admins = raw.get("admin_user_ids")
    admins = tuple(
        dict.fromkeys(
            str(item or "").strip()
            for item in raw_admins
            if str(item or "").strip()
        )
    ) if isinstance(raw_admins, (list, tuple)) else ()
    return InviteConfig(
        admin_user_ids=admins,
        mappings=mappings,
        require_direct_chat=_as_bool(raw.get("require_direct_chat"), default=True),
        response_prefix=str(raw.get("response_prefix") or "").strip(),
    )


def _normalize_keyword(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _chatroom_label(chatroom: Chatroom) -> str:
    return str(chatroom.remark or chatroom.name or chatroom.external_id).strip()


def _as_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
