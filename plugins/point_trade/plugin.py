from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    Message,
    PointsAccount,
)
from app.services.points import BAILOUT_COST, PointsService
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore


PLUGIN_CODE = "point_trade"
COMMAND_TRANSFER = "积分转账"
COMMAND_MY_POINTS = "我的积分"
COMMAND_RANKING = "积分排行"
COMMAND_ROB = "打劫"
COMMAND_BAILOUT = "保释"
COMMANDS = (COMMAND_TRANSFER, COMMAND_MY_POINTS, COMMAND_RANKING, COMMAND_ROB, COMMAND_BAILOUT)
STATE_NAMESPACE = "point_trade"
ROB_TAX_RATE = 0.2
PRISON_HOURS = 24


@dataclass(frozen=True)
class PointTradeConfig:
    """保存当前绑定的打劫规则和积分门槛。"""

    ranking_limit: int
    rob_success_rate: float
    rob_min_percent: float
    rob_max_percent: float
    rob_penalty_percent: float
    rob_min_points: int
    rob_cooldown: int


@dataclass(frozen=True)
class TrustedTradeContext:
    """保存经过数据库交叉验证的群消息和发起人。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message
    sender_id: str
    sender_name: str
    group_id: str


@dataclass(frozen=True)
class TargetUser:
    """表示在当前群内唯一解析到的积分操作目标。"""

    user_id: str
    user_name: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行时仅提供健康检查，禁止运维接口伪造积分交易。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="积分交易插件运行正常。",
            output={
                "commands": list(COMMANDS),
                "rob_success_rate": config.rob_success_rate,
                "rob_cooldown": config.rob_cooldown,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在群聊中使用积分交易命令。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """从真实群消息解析积分命令，并只操作当前群的积分账户。"""

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    config = _load_config(request.config)
    trusted, error = _require_trusted_group_message(db, request.context)
    if error is not None or trusted is None:
        return error or _error_result("当前群聊消息上下文无效。", "trusted_context_required")

    command, arguments = _parse_command(str(trusted.message.content or ""))
    if command == COMMAND_MY_POINTS and not arguments:
        return _handle_my_points(db, trusted)
    if command == COMMAND_RANKING and not arguments:
        return _handle_ranking(db, trusted, config)
    if command == COMMAND_TRANSFER:
        return _handle_transfer(db, trusted, arguments)
    if command == COMMAND_ROB:
        return _handle_rob(db, trusted, config, arguments)
    if command == COMMAND_BAILOUT:
        return _handle_bailout(db, trusted, arguments)
    return _help_result()


def _handle_transfer(db: Session, trusted: TrustedTradeContext, arguments: str) -> PluginExecutionResult:
    """按“积分转账 数量 用户”格式完成消息级幂等转账。"""

    parts = arguments.split()
    if len(parts) != 2:
        return _error_result("命令格式：积分转账 积分数 用户。", "command_invalid")
    try:
        amount = int(parts[0])
    except ValueError:
        amount = 0
    if amount <= 0:
        return _error_result("转账积分必须是大于 0 的整数。", "amount_invalid")
    target, target_error = _resolve_target_user(db, trusted, parts[1])
    if target_error is not None or target is None:
        return target_error or _error_result("未找到转账目标。", "target_not_found")
    if target.user_id == trusted.sender_id:
        return _error_result("不能给自己转账。", "self_target")

    transfer_key = _message_business_key(trusted, "transfer")
    result = PointsService(db).transfer_points(
        trusted.sender_id,
        target.user_id,
        trusted.group_id,
        amount,
        from_user_name=trusted.sender_name,
        to_user_name=target.user_name,
        description="积分转账",
        source="trade",
        metadata={"conversation_id": int(trusted.conversation.id), "message_id": int(trusted.message.id)},
        idempotency_key=transfer_key,
    )
    if not result.ok:
        db.rollback()
        return _error_result(result.message, "transfer_failed")
    extra = result.extra or {}
    from_user = extra.get("from_user") if isinstance(extra.get("from_user"), dict) else {}
    to_user = extra.get("to_user") if isinstance(extra.get("to_user"), dict) else {}
    reply_text = (
        f"积分转账成功。\n"
        f"{trusted.sender_name} 转给 {target.user_name} {amount} 积分。\n"
        f"{trusted.sender_name} 当前积分 {int(from_user.get('total_points') or 0)}。\n"
        f"{target.user_name} 当前积分 {int(to_user.get('total_points') or 0)}。"
    )
    return _text_result(
        reply_text,
        "transfer_completed",
        from_user=from_user,
        to_user=to_user,
        amount=amount,
    )


def _handle_my_points(db: Session, trusted: TrustedTradeContext) -> PluginExecutionResult:
    """查询当前发起人在当前群的积分与最近流水。"""

    service = PointsService(db)
    account = service.get_user_points(trusted.sender_id, trusted.group_id)
    if account is None:
        return _error_result("还没有积分记录，请先参与签到等积分活动。", "account_not_found")
    transactions = service.get_user_transactions(trusted.sender_id, trusted.group_id, 5)
    lines = [f"{trusted.sender_name} 当前积分 {int(account.get('total_points') or 0)}。"]
    if transactions:
        lines.append("最近流水：")
        for item in transactions:
            delta = int(item.get("points") or 0)
            sign = "+" if delta > 0 else ""
            description = str(item.get("description") or item.get("source") or "积分变动")
            lines.append(f"{sign}{delta} {description}")
    return _text_result("\n".join(lines), "points_detail", account=account, transactions=transactions)


def _handle_ranking(
    db: Session,
    trusted: TrustedTradeContext,
    config: PointTradeConfig,
) -> PluginExecutionResult:
    """只读取当前群积分账户生成排行榜。"""

    ranking = PointsService(db).get_points_ranking(trusted.group_id, config.ranking_limit)
    ranking = [item for item in ranking if str(item.get("user_id") or "") != "SYSTEM"]
    if not ranking:
        return _error_result("当前群还没有积分排行数据。", "ranking_empty")
    lines = ["积分排行榜："]
    for index, item in enumerate(ranking, start=1):
        name = str(item.get("user_name") or item.get("user_id") or f"用户{index}")
        lines.append(f"{index}. {name} {int(item.get('total_points') or 0)} 积分")
    return _text_result("\n".join(lines), "ranking", ranking=ranking)


def _handle_rob(
    db: Session,
    trusted: TrustedTradeContext,
    config: PointTradeConfig,
    arguments: str,
) -> PluginExecutionResult:
    """按消息主键稳定决定打劫结果，并原子结算积分、税费或入狱。"""

    target_token = arguments.strip()
    if not target_token or len(target_token.split()) != 1:
        return _error_result("命令格式：打劫 用户。", "command_invalid")
    target, target_error = _resolve_target_user(db, trusted, target_token)
    if target_error is not None or target is None:
        return target_error or _error_result("未找到打劫目标。", "target_not_found")
    if target.user_id == trusted.sender_id:
        return _error_result("不能打劫自己。", "self_target")

    service = PointsService(db)
    prison_status = service.check_prison_status(trusted.sender_id, trusted.group_id)
    if prison_status:
        end_at = prison_status.get("end_at")
        remaining = "未到释放时间"
        if isinstance(end_at, datetime):
            seconds = max(int((ensure_shanghai_datetime(end_at) - now_shanghai()).total_seconds()), 0)
            remaining = f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分钟"
        return _error_result(f"你正在服刑，剩余 {remaining}。", "imprisoned")

    store = PluginStateStore(db, trusted.execution)
    cooldown_key = _cooldown_state_key(trusted)
    cooldown = store.get(STATE_NAMESPACE, cooldown_key)
    if cooldown is not None:
        expires_at = _parse_datetime(cooldown.get("expires_at"))
        seconds = max(int((expires_at - now_shanghai()).total_seconds()), 0) if expires_at else config.rob_cooldown
        return _error_result(f"打劫冷却中，还需等待 {seconds} 秒。", "rob_cooldown")

    robber = service.get_user_points(trusted.sender_id, trusted.group_id)
    target_account = service.get_user_points(target.user_id, trusted.group_id)
    if robber is None or target_account is None:
        return _error_result("双方都需要先拥有积分记录。", "account_not_found")
    robber_points = int(robber.get("total_points") or 0)
    target_points = int(target_account.get("total_points") or 0)
    if robber_points < config.rob_min_points:
        return _error_result(f"你的积分不足 {config.rob_min_points}，无法打劫。", "robber_points_insufficient")
    if target_points < config.rob_min_points:
        return _error_result(f"目标积分不足 {config.rob_min_points}，不值得打劫。", "target_points_insufficient")

    current = now_shanghai()
    if config.rob_cooldown > 0:
        store.put(
            STATE_NAMESPACE,
            cooldown_key,
            {
                "message_id": int(trusted.message.id),
                "expires_at": (current + timedelta(seconds=config.rob_cooldown)).isoformat(),
            },
            ttl_seconds=config.rob_cooldown,
            now=current,
        )
    business_key = _message_business_key(trusted, "rob")
    if _deterministic_unit(trusted, "outcome") < config.rob_success_rate:
        percent_unit = _deterministic_unit(trusted, "amount")
        rob_percent = config.rob_min_percent + (config.rob_max_percent - config.rob_min_percent) * percent_unit
        robbed_points = max(2, int(target_points * rob_percent))
        # 抽成至少为 1，且总额至少为 2，保证打劫者和系统都能获得正整数流水。
        tax_points = max(1, int(robbed_points * ROB_TAX_RATE))
        actual_points = robbed_points - tax_points
        result = service.settle_robbery_success(
            trusted.sender_id,
            target.user_id,
            trusted.group_id,
            robbed_points=robbed_points,
            tax_points=tax_points,
            robber_user_name=trusted.sender_name,
            target_user_name=target.user_name,
            idempotency_key=business_key,
        )
        if not result.ok:
            db.rollback()
            return _error_result(result.message, "rob_settlement_failed")
        extra = result.extra or {}
        robber_after = extra.get("robber") if isinstance(extra.get("robber"), dict) else {}
        reply_text = (
            f"打劫成功。\n"
            f"{trusted.sender_name} 从 {target.user_name} 获得 {robbed_points} 积分。\n"
            f"实际到手 {actual_points}，系统结算 {tax_points}。\n"
            f"当前积分 {int(robber_after.get('total_points') or 0)}。"
        )
        return _text_result(
            reply_text,
            "rob_success",
            robbed_points=robbed_points,
            amount=actual_points,
            tax_amount=tax_points,
        )

    penalty_points = max(1, int(robber_points * config.rob_penalty_percent))
    result = service.settle_robbery_failure(
        trusted.sender_id,
        target.user_id,
        trusted.group_id,
        penalty_points=penalty_points,
        prison_hours=PRISON_HOURS,
        robber_user_name=trusted.sender_name,
        target_user_name=target.user_name,
        idempotency_key=business_key,
    )
    if not result.ok:
        db.rollback()
        return _error_result(result.message, "rob_settlement_failed")
    reply_text = (
        f"打劫失败。\n"
        f"你被扣除 {penalty_points} 积分，并进入 {PRISON_HOURS} 小时冷静期。\n"
        f"其他群成员可花费 {BAILOUT_COST} 积分保释。"
    )
    return _text_result(reply_text, "rob_failed", penalty_amount=penalty_points, prison_hours=PRISON_HOURS)


def _handle_bailout(db: Session, trusted: TrustedTradeContext, arguments: str) -> PluginExecutionResult:
    """按消息级幂等键原子扣费并释放当前群目标用户。"""

    target_token = arguments.strip()
    if not target_token or len(target_token.split()) != 1:
        return _error_result("命令格式：保释 用户。", "command_invalid")
    target, target_error = _resolve_target_user(db, trusted, target_token)
    if target_error is not None or target is None:
        return target_error or _error_result("未找到保释目标。", "target_not_found")
    if target.user_id == trusted.sender_id:
        return _error_result("不能保释自己。", "self_target")

    result = PointsService(db).bailout_user(
        target.user_id,
        trusted.sender_id,
        trusted.group_id,
        prisoner_user_name=target.user_name,
        bailout_user_name=trusted.sender_name,
        idempotency_key=_message_business_key(trusted, "bailout"),
    )
    if not result.ok:
        db.rollback()
        return _error_result(result.message, "bailout_failed")
    account = result.account or {}
    reply_text = (
        f"保释成功。\n"
        f"{trusted.sender_name} 花费 {BAILOUT_COST} 积分保释了 {target.user_name}。\n"
        f"当前积分 {int(account.get('total_points') or 0)}。"
    )
    return _text_result(
        reply_text,
        "bailout_success",
        cost=BAILOUT_COST,
        points=int(account.get("total_points") or 0),
    )


def _resolve_target_user(
    db: Session,
    trusted: TrustedTradeContext,
    raw_token: str,
) -> tuple[TargetUser | None, PluginExecutionResult | None]:
    """用当前群积分账户和标准群成员表解析唯一目标，不读取 provider 原始协议。"""

    token = str(raw_token or "").strip().lstrip("@").strip()
    if not token or len(token) > 128:
        return None, _error_result("目标用户格式无效。", "target_invalid")
    candidates: dict[str, str] = {}
    accounts = db.scalars(
        select(PointsAccount).where(
            PointsAccount.group_id == trusted.group_id,
            or_(PointsAccount.user_id == token, PointsAccount.user_name == token),
        )
    ).all()
    for account in accounts:
        candidates[str(account.user_id)] = str(account.user_name or account.user_id)

    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.channel_id == int(trusted.channel.id),
            Chatroom.external_id == trusted.group_id,
        )
    )
    if chatroom is not None:
        members = db.scalars(
            select(ChatroomMember).where(
                ChatroomMember.chatroom_id == int(chatroom.id),
                ChatroomMember.status == "active",
                or_(
                    ChatroomMember.member_external_id == token,
                    ChatroomMember.nickname == token,
                    ChatroomMember.display_name == token,
                ),
            )
        ).all()
        for member in members:
            user_id = str(member.member_external_id or "").strip()
            if user_id:
                candidates[user_id] = str(member.display_name or member.nickname or user_id).strip() or user_id

    if not candidates:
        return None, _error_result("当前群没有找到该用户。", "target_not_found")
    if len(candidates) > 1:
        return None, _error_result("当前群存在同名用户，请改用唯一账号 ID。", "target_ambiguous")
    user_id, user_name = next(iter(candidates.items()))
    return TargetUser(user_id=user_id, user_name=user_name), None


def _require_trusted_group_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedTradeContext | None, PluginExecutionResult | None]:
    """交叉验证绑定、渠道、群会话、入站消息和发送人。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
        or context.message_type != "text"
    ):
        return None, _error_result("请在群聊中使用积分交易命令。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
    sender_name = str(message.sender_name or sender_id).strip() if message is not None else ""
    group_id = str(conversation.external_id or "").strip() if conversation is not None else ""
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
        or not group_id
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "") != "in"
        or str(message.message_type or "") != "text"
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
    ):
        return None, _error_result("当前群聊消息上下文无效。", "trusted_context_required")
    return TrustedTradeContext(
        execution=context,
        binding=binding,
        channel=channel,
        conversation=conversation,
        message=message,
        sender_id=sender_id,
        sender_name=sender_name or sender_id,
        group_id=group_id,
    ), None


def _load_config(raw_config: Mapping[str, Any]) -> PointTradeConfig:
    """读取唯一配置字段，并限制概率、比例和数量范围。"""

    raw = dict(raw_config)
    min_percent = _bounded_float(raw.get("rob_min_percent"), default=0.2, minimum=0.01, maximum=0.8)
    max_percent = _bounded_float(raw.get("rob_max_percent"), default=0.4, minimum=min_percent, maximum=0.9)
    return PointTradeConfig(
        ranking_limit=_bounded_int(raw.get("ranking_limit"), default=10, minimum=3, maximum=30),
        rob_success_rate=_bounded_float(raw.get("rob_success_rate"), default=0.4, minimum=0.0, maximum=1.0),
        rob_min_percent=min_percent,
        rob_max_percent=max_percent,
        rob_penalty_percent=_bounded_float(raw.get("rob_penalty_percent"), default=0.1, minimum=0.01, maximum=0.9),
        rob_min_points=_bounded_int(raw.get("rob_min_points"), default=30, minimum=1, maximum=1_000_000),
        rob_cooldown=_bounded_int(raw.get("rob_cooldown"), default=300, minimum=0, maximum=86_400),
    )


def _parse_command(content: str) -> tuple[str, str]:
    """只解析五个当前中文命令，不保留旧别名和英文入口。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].strip()
    parts = normalized.split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) == 2 else ""


def _message_business_key(trusted: TrustedTradeContext, action: str) -> str:
    """按绑定、群会话和消息主键生成稳定积分业务键。"""

    return (
        f"point-trade:{int(trusted.binding.id)}:{int(trusted.conversation.id)}:"
        f"{int(trusted.message.id)}:{action}"
    )


def _cooldown_state_key(trusted: TrustedTradeContext) -> str:
    """用发送人摘要生成符合状态存储字符约束的冷却键。"""

    sender_digest = hashlib.sha256(trusted.sender_id.encode("utf-8")).hexdigest()[:24]
    return f"rob-cooldown:{int(trusted.conversation.id)}:{sender_digest}"


def _deterministic_unit(trusted: TrustedTradeContext, salt: str) -> float:
    """从可信消息身份派生稳定的 0 到 1 随机数，重试时结果不变化。"""

    raw = (
        f"{int(trusted.execution.tenant_id)}:{int(trusted.binding.id)}:"
        f"{int(trusted.conversation.id)}:{int(trusted.message.id)}:{salt}"
    ).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    return value / float(2**64)


def _parse_datetime(value: Any) -> datetime | None:
    """读取插件状态中的 ISO 时间，损坏值视为无剩余时间信息。"""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return ensure_shanghai_datetime(datetime.fromisoformat(normalized.replace("Z", "+00:00")))
    except ValueError:
        return None


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """把租户整数配置限制在业务允许区间。"""

    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    """把租户概率和比例配置限制在业务允许区间。"""

    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    """只接受明确布尔语义，避免非空字符串误判为启用。"""

    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _help_result() -> PluginExecutionResult:
    """返回面向群用户的最小命令格式。"""

    text = (
        "积分命令：\n"
        "积分转账 积分数 用户\n"
        "我的积分\n"
        "积分排行\n"
        "打劫 用户\n"
        "保释 用户"
    )
    return _text_result(text, "help")


def _text_result(text: str, provider_status: str, **output: Any) -> PluginExecutionResult:
    """构造统一文本回复和业务输出。"""

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text,
        output={
            "provider_status": provider_status,
            **output,
            "reply_text": text,
            "replies": standard_replies([text_reply(text)]),
        },
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """返回不暴露数据库和积分内部实现的业务错误。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status},
    )
