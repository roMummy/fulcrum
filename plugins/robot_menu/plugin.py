from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.plugin_scopes import group_plugin_scope_enabled
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Message, Plugin
from app.services.plugin_directory import DiscoveredPlugin, get_discovered_plugin
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply


PLUGIN_CODE = "robot_menu"
COMMANDS = ("菜单", "功能菜单")
MANAGED_GROUP_CHANNEL_TYPES = {"wechat_ipad", "wechat_winauto", "workwechat", "wechat_webridge"}
NON_USER_PLUGIN_TYPES = {"system", "scheduled", "timer", "cron"}


@dataclass(frozen=True)
class RobotMenuConfig:
    """保存菜单插件唯一生效的显示配置。"""

    max_items: int


@dataclass(frozen=True)
class TrustedMenuContext:
    """保存经过数据库交叉验证的当前群消息和插件绑定。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message
    chatroom: Chatroom | None


@dataclass(frozen=True)
class MenuItem:
    """保存一个普通群成员可以实际触发的插件菜单项。"""

    plugin_code: str
    display_name: str
    commands: tuple[str, ...]
    keywords: tuple[str, ...]
    require_mention: bool


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行入口只提供健康检查，菜单内容必须来自真实群会话。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="功能菜单插件运行正常。",
            output={"commands": list(COMMANDS), "max_items": config.max_items},
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在群聊中发送“菜单”查看可用功能。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """从真实群消息生成当前机器人和当前群的可用功能菜单。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    trusted, context_error = _require_trusted_group_message(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _error_result("当前群聊消息上下文无效。", "trusted_context_required")

    command, arguments = _parse_command(str(trusted.message.content or ""))
    if command not in COMMANDS or arguments:
        return _error_result("请直接发送“菜单”或“功能菜单”。", "command_invalid")
    items = _list_menu_items(db, trusted, maximum=config.max_items)
    reply_text = _render_menu(items)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已展示 {len(items)} 个可用功能。",
        output={
            "provider_status": "completed",
            "item_count": len(items),
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def _list_menu_items(
    db: Session,
    trusted: TrustedMenuContext,
    *,
    maximum: int,
) -> list[MenuItem]:
    """读取启用绑定，并应用渠道能力、绑定范围和当前群强制停用设置。"""

    bindings = db.scalars(
        select(BotPlugin)
        .join(Plugin, Plugin.id == BotPlugin.plugin_id)
        .where(
            BotPlugin.bot_id == int(trusted.execution.bot_id),
            BotPlugin.status == "enabled",
        )
        .order_by(BotPlugin.id.asc())
    ).all()
    scope_map = _load_scope_map(db, trusted)
    items_by_code: dict[str, MenuItem] = {}
    for binding in bindings:
        plugin_code = str(binding.plugin.code or "").strip() if binding.plugin is not None else ""
        discovered = get_discovered_plugin(plugin_code)
        if discovered is None or not discovered.is_valid:
            continue
        if not _plugin_is_visible(binding, discovered, trusted, scope_map.get(int(binding.id))):
            continue

        binding_config = _parse_json_object(binding.config_json)
        commands = _normalize_text_tuple(binding_config.get("commands")) or discovered.matcher.commands
        keywords = _normalize_text_tuple(binding_config.get("keywords")) or discovered.matcher.keywords
        if not commands and not keywords:
            continue
        items_by_code[plugin_code] = MenuItem(
            plugin_code=plugin_code,
            display_name=str(discovered.manifest.display_name or plugin_code).strip() or plugin_code,
            commands=commands[:5],
            keywords=keywords[:5],
            require_mention=_as_bool(
                binding_config.get("require_mention"),
                default=discovered.matcher.require_mention,
            ),
        )

    items = sorted(items_by_code.values(), key=lambda item: (item.display_name, item.plugin_code))
    return items[: max(1, min(int(maximum), 50))]


def _plugin_is_visible(
    binding: BotPlugin,
    discovered: DiscoveredPlugin,
    trusted: TrustedMenuContext,
    scope: GroupPluginScope | None,
) -> bool:
    """判断插件是否在当前群对普通用户真实可用。"""

    plugin_type = str(discovered.manifest.plugin_type or "").strip().lower()
    if plugin_type in NON_USER_PLUGIN_TYPES:
        return False
    if not _capability_matches(discovered, trusted):
        return False
    if not group_plugin_scope_enabled(binding, scope):
        return False

    binding_config = _parse_json_object(binding.config_json)
    scope_config = _parse_json_object(scope.config_override_json if scope is not None else None)
    effective_config = {**dict(discovered.runtime_config), **binding_config, **scope_config}
    external_id = str(trusted.conversation.external_id or "").strip()
    if not _binding_allows_conversation(binding_config, external_id):
        return False
    return True


def _capability_matches(discovered: DiscoveredPlugin, trusted: TrustedMenuContext) -> bool:
    """使用目录能力声明过滤当前渠道、群聊和文本消息。"""

    channel_types = {str(item).strip() for item in discovered.capability.channel_types if str(item).strip()}
    channel_type = str(trusted.channel.channel_type or "").strip()
    if channel_types and channel_type not in channel_types:
        # 平台把历史只声明 simulator 的无协议插件视为可在 Telegram 文本通道运行，
        # 菜单必须与真实消息分发保持相同判断，不能展示后又无法触发。
        if not (channel_type == "telegram" and "simulator" in channel_types):
            return False
    if discovered.capability.chat_types and "group" not in discovered.capability.chat_types:
        return False
    if discovered.capability.message_types and "text" not in discovered.capability.message_types:
        return False
    return True


def _load_scope_map(db: Session, trusted: TrustedMenuContext) -> dict[int, GroupPluginScope]:
    """一次读取当前群的所有插件范围，避免为每个菜单项重复查询。"""

    if trusted.chatroom is None:
        return {}
    rows = db.scalars(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == int(trusted.channel.id),
            GroupPluginScope.chatroom_id == int(trusted.chatroom.id),
        )
    ).all()
    return {int(row.bot_plugin_id): row for row in rows}


def _require_trusted_group_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedMenuContext | None, PluginExecutionResult | None]:
    """交叉验证菜单绑定、渠道、群会话、入站消息和群级范围。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
        or context.message_type != "text"
    ):
        return None, _error_result("请在群聊中发送“菜单”。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    conversation_external_id = str(conversation.external_id or "").strip() if conversation is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
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
        or not conversation_external_id
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "") != "in"
        or str(message.message_type or "") != "text"
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
    ):
        return None, _error_result("当前群聊消息上下文无效。", "trusted_context_required")
    if not _binding_allows_conversation(_parse_json_object(binding.config_json), conversation_external_id):
        return None, _error_result("当前群未启用功能菜单。", "binding_scope_disabled")

    chatroom: Chatroom | None = None
    menu_scope = None
    channel_type = str(channel.channel_type or "").strip()
    if channel_type in MANAGED_GROUP_CHANNEL_TYPES:
        chatroom = db.scalar(
            select(Chatroom).where(
                Chatroom.channel_id == int(channel.id),
                Chatroom.external_id == conversation_external_id,
            )
        )
        if chatroom is not None and not bool(chatroom.is_active):
            return None, _error_result("当前群未启用机器人功能。", "group_inactive")
        if chatroom is not None:
            menu_scope = db.scalar(
                select(GroupPluginScope).where(
                    GroupPluginScope.channel_id == int(channel.id),
                    GroupPluginScope.chatroom_id == int(chatroom.id),
                    GroupPluginScope.bot_plugin_id == int(binding.id),
                )
            )
    if not group_plugin_scope_enabled(binding, menu_scope):
        return None, _error_result("当前群未启用功能菜单。", "group_scope_disabled")
    return (
        TrustedMenuContext(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            message=message,
            chatroom=chatroom,
        ),
        None,
    )


def _render_menu(items: list[MenuItem]) -> str:
    """生成面向群成员的紧凑文本菜单，不展示技术状态或后台字段。"""

    if not items:
        return "当前群暂无可用功能。"
    lines = ["功能菜单"]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item.display_name}")
        if item.commands:
            lines.append(f"   指令：{' / '.join(item.commands)}")
        if item.keywords:
            lines.append(f"   自动触发：{' / '.join(item.keywords)}")
        if item.require_mention:
            lines.append("   使用时需 @机器人")
    return "\n".join(lines)


def _parse_command(content: str) -> tuple[str, str]:
    """只解析当前两个菜单命令，不保留状态和管理子命令。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].strip()
    parts = normalized.split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) == 2 else ""


def _normalize_text_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        raw_items = value.replace("，", ",").split(",")
    else:
        return ()
    return tuple(dict.fromkeys(str(item).strip() for item in raw_items if str(item).strip()))


def _normalize_scope_values(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip() for item in value.replace("，", ",").split(",") if item.strip()}
    return set()


def _binding_allows_conversation(config: Mapping[str, Any], external_id: str) -> bool:
    """使用平台统一的绑定允许/拒绝范围判断当前群是否可进入插件。"""

    allow_from = _normalize_scope_values(config.get("allow_from"))
    deny_from = _normalize_scope_values(config.get("deny_from"))
    return external_id not in deny_from and (not allow_from or external_id in allow_from)


def _parse_json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_config(raw_config: Mapping[str, Any]) -> RobotMenuConfig:
    raw = dict(raw_config)
    return RobotMenuConfig(
        max_items=_bounded_int(raw.get("max_items"), default=30, minimum=1, maximum=50),
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
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


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """返回文本错误，不暴露插件绑定、群编号或后台配置。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
