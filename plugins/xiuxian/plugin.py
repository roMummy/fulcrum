from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Channel, Conversation, Message
from app.services.cultivation import (
    CultivationCommandContext,
    CultivationResult,
    CultivationRules,
    CultivationService,
)
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply


PLUGIN_CODE = "xiuxian"
COMMANDS = (
    "修仙帮助",
    "注册修仙",
    "我的状态",
    "闭关",
    "出关",
    "聚灵",
    "排行榜",
    "修仙签到",
    "坊市",
    "购买",
    "乾坤袋",
    "使用",
    "服用",
    "突破",
    "强行突破",
    "积分购石",
    "积分换灵石",
    "劫掠",
    "赠与",
    "赠送",
    "创建门派",
    "加入门派",
    "退出门派",
    "出门历练",
    "炼丹",
)


@dataclass(frozen=True)
class XiuxianConfig:
    """保存当前租户机器人绑定唯一生效的修仙规则。"""

    rules: CultivationRules


@dataclass(frozen=True)
class TrustedXiuxianMessage:
    """保存经过数据库交叉验证的插件绑定、群会话和真实入站消息。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message


def matches_event(context) -> dict[str, object]:
    """只匹配正式群文本命令，不接收旧别名、私聊或渠道私有消息类型。"""

    config = _load_config(getattr(context, "binding_config", {}) or {})
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    command, _arguments = _parse_command(str(getattr(context, "content", "") or ""))
    matched = chat_type == "group" and message_type == "text" and command in COMMANDS
    return {
        "matched": matched,
        "reason": "xiuxian_command_hit" if matched else "xiuxian_group_command_required",
        "debug": {"command": command, "chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行入口只提供重载状态，修仙业务必须来自真实群消息。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="文字修仙插件已就绪。",
            output={
                "commands": list(COMMANDS),
                "point_to_stone_rate": config.rules.point_to_stone_rate,
                "ranking_limit": config.rules.ranking_limit,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _plugin_error("请在群聊中发送修仙命令。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """验证可信群消息后，把唯一业务路径交给修仙事务服务执行。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    trusted, error = _require_trusted_group_message(db, request.context)
    if error is not None or trusted is None:
        return error or _plugin_error("当前修仙群消息上下文无效。", "trusted_context_required")

    # 命令和参数只读取平台落库后的真实 Message。request.payload 属于插件业务
    # 扩展载荷，不能覆盖发送人、群、消息内容或其他资产结算身份。
    command, arguments = _parse_command(str(trusted.message.content or ""))
    if command not in COMMANDS:
        return _plugin_error("当前消息不是正式修仙命令。", "command_invalid")
    sender_id = str(trusted.message.sender_id or "").strip()
    sender_name = str(trusted.message.sender_name or sender_id).strip() or sender_id
    context = CultivationCommandContext(
        tenant_id=int(trusted.execution.tenant_id),
        binding_id=int(trusted.binding.id),
        conversation_id=int(trusted.conversation.id),
        message_id=int(trusted.message.id),
        member_external_id=sender_id,
        display_name=sender_name,
        group_external_id=str(trusted.conversation.external_id or ""),
    )
    result = CultivationService(db, context).execute(command, arguments, config.rules)
    return _business_result(command, result)


def _require_trusted_group_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedXiuxianMessage | None, PluginExecutionResult | None]:
    """确认所有主键指向同一租户、机器人、渠道、群和入站消息。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
    ):
        return None, _plugin_error("修仙命令缺少真实群消息。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""

    # 这里不根据 payload 或外部账号重新查找资源。任一归属不一致都直接拒绝，
    # 防止攻击者拼接其他租户的渠道、会话或消息主键修改修仙资产。
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
        or str(conversation.chat_type or "").strip().lower() != "group"
        or not str(conversation.external_id or "").strip()
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip().lower() != "in"
        or str(message.message_type or "").strip().lower() != "text"
        or not str(message.sender_id or "").strip()
        or not str(message.content or "").strip()
    ):
        return None, _plugin_error("当前修仙群消息上下文无效。", "trusted_context_required")
    if context.sender_id is not None and str(context.sender_id).strip() != str(message.sender_id).strip():
        return None, _plugin_error("当前修仙命令发送人上下文无效。", "trusted_context_required")
    return TrustedXiuxianMessage(context, binding, channel, conversation, message), None


def _load_config(raw: Mapping[str, Any]) -> XiuxianConfig:
    """限制租户可调参数范围，并组装修仙服务使用的明确规则对象。"""

    register_cooldown = _bounded_int(raw.get("register_cooldown_seconds"), 5, 0, 604_800)
    inout_cooldown = _bounded_int(raw.get("inout_cooldown_seconds"), 5, 0, 604_800)
    use_cooldown = _bounded_int(raw.get("use_cooldown_seconds"), 3, 0, 604_800)
    breakthrough_cooldown = _bounded_int(raw.get("breakthrough_cooldown_seconds"), 60, 0, 604_800)
    gift_cooldown = _bounded_int(raw.get("gift_cooldown_seconds"), 10, 0, 604_800)
    clan_cooldown = _bounded_int(raw.get("clan_cooldown_seconds"), 10, 0, 604_800)
    points_cooldown = _bounded_int(raw.get("points_exchange_cooldown_seconds"), 10, 0, 604_800)
    cooldowns = {
        "注册修仙": register_cooldown,
        "闭关": inout_cooldown,
        "出关": inout_cooldown,
        "聚灵": _bounded_int(raw.get("gather_cooldown_seconds"), 30, 0, 604_800),
        "购买": _bounded_int(raw.get("buy_cooldown_seconds"), 5, 0, 604_800),
        "使用": use_cooldown,
        "服用": use_cooldown,
        "突破": breakthrough_cooldown,
        "强行突破": breakthrough_cooldown,
        "劫掠": _bounded_int(raw.get("rob_cooldown_seconds"), 30, 0, 604_800),
        "赠与": gift_cooldown,
        "赠送": gift_cooldown,
        "创建门派": clan_cooldown,
        "加入门派": clan_cooldown,
        "退出门派": clan_cooldown,
        "积分购石": points_cooldown,
        "积分换灵石": points_cooldown,
    }
    return XiuxianConfig(
        rules=CultivationRules(
            max_cultivate_hours=_bounded_int(raw.get("max_cultivate_hours"), 8, 1, 24),
            base_rate_per_hour=_bounded_int(raw.get("base_rate_per_hour"), 100, 1, 1_000_000),
            unstable_minutes=_bounded_int(raw.get("unstable_minutes"), 15, 1, 1_440),
            injured_minutes=_bounded_int(raw.get("injured_minutes"), 60, 1, 10_080),
            point_to_stone_rate=_bounded_int(raw.get("point_to_stone_rate"), 10, 1, 1_000_000),
            ranking_limit=_bounded_int(raw.get("ranking_limit"), 10, 1, 50),
            sign_reward_stones=_bounded_int(raw.get("sign_reward_stones"), 50, 1, 1_000_000),
            expedition_cooldown_seconds=_bounded_int(
                raw.get("expedition_cooldown_seconds"),
                86_400,
                60,
                604_800,
            ),
            alchemy_cooldown_seconds=_bounded_int(
                raw.get("alchemy_cooldown_seconds"),
                600,
                1,
                86_400,
            ),
            cooldowns=cooldowns,
        ),
    )


def _parse_command(content: str) -> tuple[str, str]:
    """按最长正式命令解析参数，只接受平台统一的斜杠或井号前缀。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    for command in sorted(COMMANDS, key=len, reverse=True):
        if normalized == command:
            return command, ""
        if normalized.startswith(f"{command} "):
            return command, normalized[len(command) :].strip()
    return "", ""


def _business_result(command: str, result: CultivationResult) -> PluginExecutionResult:
    """把渠道无关的业务文本转换为平台标准回复。"""

    return PluginExecutionResult(
        ok=result.ok,
        status="completed" if result.ok else "rejected",
        message=result.text,
        output={
            "provider_status": result.code,
            "command": command,
            "reply_text": result.text,
            "details": dict(result.details),
            "replies": standard_replies([text_reply(result.text)]),
        },
    )


def _plugin_error(message: str, provider_status: str) -> PluginExecutionResult:
    """返回进入业务服务前的可信上下文或插件配置错误。"""

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


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}
