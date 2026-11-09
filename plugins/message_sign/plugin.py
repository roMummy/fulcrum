from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Conversation, Message, SignInHistory, SignInRecord
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply


PLUGIN_CODE = "message_sign"
SIGN_COMMAND = "签到"
MAKEUP_COMMAND = "补签"


@dataclass(frozen=True)
class SignConfig:
    """保存当前绑定可调整的签到积分规则。"""

    min_point: int
    max_point: int
    streak_cycle: int
    max_streak_point: int
    makeup_cost: int


@dataclass(frozen=True)
class TrustedSignContext:
    """保存经过数据库交叉验证的群聊、消息和发送人。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message
    sender_id: str
    sender_name: str
    group_id: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行时仅提供健康检查，业务执行必须来自可信群消息。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群签到插件运行正常。",
            output={
                "commands": [SIGN_COMMAND, MAKEUP_COMMAND],
                "makeup_cost": config.makeup_cost,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在群聊中发送签到或补签。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """从真实群文本消息解析签到命令，并在当前租户事务中更新状态。"""

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    config = _load_config(request.config)
    trusted, error = _require_trusted_group_message(db, request.context)
    if error is not None or trusted is None:
        return error or _error_result("当前群聊消息上下文无效。", "trusted_context_required")

    command = _parse_command(str(trusted.message.content or ""))
    if command == SIGN_COMMAND:
        return _handle_sign(db, trusted, config)
    if command == MAKEUP_COMMAND:
        return _handle_makeup_sign(db, trusted, config)
    return _error_result("当前只支持签到和补签。", "command_invalid")


def _handle_sign(db: Session, trusted: TrustedSignContext, config: SignConfig) -> PluginExecutionResult:
    """记录当日签到，并通过消息级幂等键发放一次积分奖励。"""

    current = now_shanghai()
    today_start = _start_of_day(current)
    yesterday_start = today_start - timedelta(days=1)
    record = _find_sign_record(db, trusted.sender_id, trusted.group_id)
    previous_sign = _normalize_datetime(record.sign_stat, current.tzinfo) if record and record.sign_stat else None
    if previous_sign is not None and previous_sign >= today_start:
        account = PointsService(db).get_user_points(trusted.sender_id, trusted.group_id) or {}
        current_points = int(account.get("total_points") or 0)
        return _text_result(
            f"今天已经签过到了。\n累计签到 {int(record.total_sign_count or 0)} 次，当前积分 {current_points}。",
            "already_signed",
            user_id=trusted.sender_id,
            group_id=trusted.group_id,
            total_sign_count=int(record.total_sign_count or 0),
            points=current_points,
        )

    old_streak = int(record.signin_streak or 0) if record is not None else 0
    continuous = previous_sign is not None and yesterday_start <= previous_sign < today_start
    streak = old_streak + 1 if continuous else 1
    streak_broken = previous_sign is not None and previous_sign < yesterday_start
    points_awarded = _calculate_points(config, streak)
    todays_rank = _count_signs_for_day(db, trusted.group_id, today_start, today_start + timedelta(days=1)) + 1

    if record is None:
        record = SignInRecord(
            user_id=trusted.sender_id,
            group_id=trusted.group_id,
            user_name=trusted.sender_name,
            points=0,
            total_sign_count=0,
            signin_streak=0,
            previous_streak=0,
        )
        db.add(record)
        db.flush()

    record.user_name = trusted.sender_name
    record.last_sign_date = record.sign_stat
    record.sign_stat = current
    record.signin_streak = streak
    # 断签前的天数需要完整保留。即使此前只有一天，也可能在今天签到后通过
    # 补签昨天恢复成三天连续记录，不能只在旧连签大于一时保存。
    record.previous_streak = old_streak if streak_broken and old_streak > 0 else 0
    record.total_sign_count = int(record.total_sign_count or 0) + 1
    record.points = int(record.points or 0) + points_awarded
    db.add(
        SignInHistory(
            record_id=int(record.id),
            user_id=trusted.sender_id,
            group_id=trusted.group_id,
            signed_at=current,
            is_makeup=False,
            points_delta=points_awarded,
            streak=streak,
            note=f"sign:binding:{int(trusted.binding.id)}:message:{int(trusted.message.id)}",
        )
    )

    reward_key = (
        f"message-sign:{int(trusted.binding.id)}:{int(trusted.conversation.id)}:"
        f"{int(trusted.message.id)}:reward"
    )
    points_result = PointsService(db).earn(
        trusted.execution,
        points_awarded,
        source="checkin",
        description="签到奖励",
        metadata={"conversation_id": int(trusted.conversation.id), "message_id": int(trusted.message.id)},
        idempotency_key=reward_key,
    )
    if not points_result.ok:
        db.rollback()
        return _error_result("签到积分发放失败。", "points_failed")
    # PointsService 已提交积分和本事务中的签到状态；重复流水分支仍显式提交，
    # 确保未来并发恢复场景中状态修改不会悬留在会话内。
    db.commit()
    balance = int((points_result.account or {}).get("total_points") or 0)

    lines = [f"签到成功，获得 {points_awarded} 积分，今天第 {todays_rank} 个签到。"]
    if streak_broken and old_streak > 0:
        lines.append(f"此前连续签到 {old_streak} 天，已重新开始累计。")
    elif streak > 1:
        lines.append(f"当前连续签到 {streak} 天。")
    lines.append(f"累计签到 {int(record.total_sign_count or 0)} 次，当前积分 {balance}。")
    return _text_result(
        "\n".join(lines),
        "signed",
        user_id=trusted.sender_id,
        group_id=trusted.group_id,
        points_awarded=points_awarded,
        points=balance,
        signin_streak=streak,
        total_sign_count=int(record.total_sign_count or 0),
        todays_rank=todays_rank,
    )


def _handle_makeup_sign(db: Session, trusted: TrustedSignContext, config: SignConfig) -> PluginExecutionResult:
    """只补昨天一次，并用消息级幂等键扣除补签积分。"""

    current = now_shanghai()
    today_start = _start_of_day(current)
    yesterday_start = today_start - timedelta(days=1)
    day_before_yesterday_start = today_start - timedelta(days=2)
    record = _find_sign_record(db, trusted.sender_id, trusted.group_id)
    if record is None:
        return _error_result("还没有签到记录，无法补签。", "record_not_found")

    current_sign = _normalize_datetime(record.sign_stat, current.tzinfo) if record.sign_stat else None
    last_sign = _normalize_datetime(record.last_sign_date, current.tzinfo) if record.last_sign_date else None
    signed_today = current_sign is not None and current_sign >= today_start
    # 今天尚未签到时，sign_stat 就是最近一次真实签到；今天已经签到时，
    # last_sign_date 保存签到前的日期。两种路径最终都只允许最近签到在前天。
    eligibility_sign = last_sign if signed_today else current_sign
    if not (
        eligibility_sign is not None
        and day_before_yesterday_start <= eligibility_sign < yesterday_start
    ):
        return _error_result("当前只支持补签昨天，且仅限断签一天内。", "makeup_unavailable")

    points_service = PointsService(db)
    current_points = points_service.balance(trusted.execution)
    if current_points < config.makeup_cost:
        return _error_result(
            f"积分不足，补签需要 {config.makeup_cost} 积分，当前仅有 {current_points} 积分。",
            "points_insufficient",
        )

    if signed_today:
        previous_streak = int(record.previous_streak or 0)
        if previous_streak <= 0:
            return _error_result("签到连续记录不完整，无法恢复昨天。", "streak_state_invalid")
        # 用户今天签到前已有 previous_streak 天，补上昨天后还要包含今天，
        # 因此恢复后的连续天数是旧连续天数加两天。
        new_streak = previous_streak + 2
        record.last_sign_date = yesterday_start
    else:
        new_streak = int(record.signin_streak or 0) + 1
        record.last_sign_date = current_sign
        record.sign_stat = yesterday_start

    record.user_name = trusted.sender_name
    record.signin_streak = new_streak
    record.previous_streak = 0
    record.total_sign_count = int(record.total_sign_count or 0) + 1
    # SignInRecord.points 只累计签到奖励；补签费用属于统一积分账本支出，
    # 不能在两个字段中重复表达为负向签到奖励。
    db.add(
        SignInHistory(
            record_id=int(record.id),
            user_id=trusted.sender_id,
            group_id=trusted.group_id,
            signed_at=yesterday_start,
            is_makeup=True,
            points_delta=0,
            streak=new_streak,
            note=f"makeup:binding:{int(trusted.binding.id)}:message:{int(trusted.message.id)}",
        )
    )

    cost_key = (
        f"message-sign:{int(trusted.binding.id)}:{int(trusted.conversation.id)}:"
        f"{int(trusted.message.id)}:makeup"
    )
    deduct_result = points_service.spend(
        trusted.execution,
        config.makeup_cost,
        source="checkin_makeup",
        description="补签消耗",
        metadata={"conversation_id": int(trusted.conversation.id), "message_id": int(trusted.message.id)},
        idempotency_key=cost_key,
    )
    if not deduct_result.ok:
        db.rollback()
        return _error_result("补签积分扣除失败。", "points_failed")
    db.commit()
    balance = int((deduct_result.account or {}).get("total_points") or 0)
    reply_text = (
        f"补签成功，消耗 {config.makeup_cost} 积分。\n"
        f"连续签到恢复为 {new_streak} 天。\n"
        f"累计签到 {int(record.total_sign_count or 0)} 次，当前积分 {balance}。"
    )
    return _text_result(
        reply_text,
        "makeup_signed",
        user_id=trusted.sender_id,
        group_id=trusted.group_id,
        signin_streak=new_streak,
        total_sign_count=int(record.total_sign_count or 0),
        points=balance,
        cost=config.makeup_cost,
    )


def _require_trusted_group_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedSignContext | None, PluginExecutionResult | None]:
    """交叉验证绑定、渠道、群会话、入站消息和发送人。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
        or context.message_type != "text"
    ):
        return None, _error_result("请在群聊中发送签到或补签。", "trusted_context_required")

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
    return TrustedSignContext(
        execution=context,
        binding=binding,
        channel=channel,
        conversation=conversation,
        message=message,
        sender_id=sender_id,
        sender_name=sender_name or sender_id,
        group_id=group_id,
    ), None


def _load_config(raw_config: Mapping[str, Any]) -> SignConfig:
    """读取唯一配置字段，并为积分规则设置明确上限。"""

    raw = dict(raw_config)
    min_point = _bounded_int(raw.get("min_point"), default=3, minimum=1, maximum=10_000)
    max_point = _bounded_int(raw.get("max_point"), default=50, minimum=min_point, maximum=100_000)
    return SignConfig(
        min_point=min_point,
        max_point=max_point,
        streak_cycle=_bounded_int(raw.get("streak_cycle"), default=1, minimum=1, maximum=365),
        max_streak_point=_bounded_int(raw.get("max_streak_point"), default=50, minimum=0, maximum=100_000),
        makeup_cost=_bounded_int(raw.get("makeup_cost"), default=10, minimum=1, maximum=1_000_000),
    )


def _parse_command(content: str) -> str:
    """只解析签到和补签两个当前命令，不保留英文或历史别名。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].strip()
    return normalized.split(maxsplit=1)[0] if normalized else ""


def _find_sign_record(db: Session, user_id: str, group_id: str) -> SignInRecord | None:
    """在当前租户会话中读取用户在当前群的签到记录。"""

    return db.scalar(
        select(SignInRecord)
        .where(SignInRecord.user_id == user_id, SignInRecord.group_id == group_id)
        .order_by(SignInRecord.id.asc())
        .limit(1)
    )


def _calculate_points(config: SignConfig, streak: int) -> int:
    """按连续天数计算奖励，并受单次奖励上限约束。"""

    extra = min(max(streak, 1) // config.streak_cycle, config.max_streak_point)
    return min(config.min_point + extra, config.max_point)


def _count_signs_for_day(db: Session, group_id: str, start: datetime, end: datetime) -> int:
    """直接从租户签到历史计算群内当日真实签到排名。"""

    count = db.scalar(
        select(func.count(SignInHistory.id)).where(
            SignInHistory.group_id == group_id,
            SignInHistory.is_makeup.is_(False),
            SignInHistory.signed_at >= start,
            SignInHistory.signed_at < end,
        )
    )
    return int(count or 0)


def _start_of_day(value: datetime) -> datetime:
    """生成与当前上海时间同一时区的当天零点。"""

    return datetime.combine(value.date(), time.min, tzinfo=value.tzinfo)


def _normalize_datetime(value: datetime | None, tzinfo: timezone | ZoneInfo | None) -> datetime | None:
    """把数据库中的可选时间统一到当前业务时区。"""

    if value is None:
        return None
    effective_tz = tzinfo or timezone(timedelta(hours=8))
    if value.tzinfo is None:
        return value.replace(tzinfo=effective_tz)
    return value.astimezone(effective_tz)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    """把租户配置限制在业务允许的整数区间。"""

    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    """只接受明确布尔语义，避免非空字符串被误判为启用。"""

    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


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
