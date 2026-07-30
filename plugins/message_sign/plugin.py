from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.time import now_shanghai
from app.models.entities import SignInHistory, SignInRecord
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_SIGN_COMMANDS = ("签到", "每日签到", "qd", "Qd", "QD", "上班", "牛马", "sign", "checkin", "message_sign")
DEFAULT_MAKEUP_COMMANDS = ("补签", "续签", "makeup")


@dataclass(frozen=True)
class SignConfig:
    enabled: bool
    timezone: str
    min_point: int
    max_point: int
    streak_cycle: int
    max_streak_point: int
    makeup_cost: int
    sign_commands: tuple[str, ...]
    makeup_commands: tuple[str, ...]


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="message_sign runtime is ready",
            output={
                "enabled": config.enabled,
                "timezone": config.timezone,
                "commands": list(config.sign_commands),
                "makeup_commands": list(config.makeup_commands),
                "makeup_cost": config.makeup_cost,
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not config.enabled:
        return PluginExecutionResult(ok=False, status="disabled", message="签到插件当前未启用。")

    command = _resolve_command(request.payload)
    if command in config.makeup_commands:
        return _handle_makeup_sign(request, config)
    return _handle_sign(request, config)


def _handle_sign(request: PluginExecutionRequest, config: SignConfig) -> PluginExecutionResult:
    now = _now(config.timezone)
    sender_id = _resolve_sender_id(request.payload)
    if not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="签到缺少 sender_id/user。")
    room_id = _resolve_room_id(request.payload)
    sender_name = _resolve_sender_name(request.payload, sender_id)
    today_start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    yesterday_start = today_start - timedelta(days=1)

    with SessionLocal() as db:
        points_service = PointsService(db)
        record = db.scalar(select(SignInRecord).where(SignInRecord.user_id == sender_id, SignInRecord.group_id == room_id))
        if record and record.sign_stat and _normalize_dt(record.sign_stat, now.tzinfo) >= today_start:
            total_count = int(record.total_sign_count or 0)
            points_account = points_service.get_user_points(sender_id, room_id) or {}
            current_points = int(points_account.get("total_points") or record.points or 0)
            reply_text = f"今天已经签过到了，累计签到 {total_count} 次，当前积分 {current_points}。"
            return PluginExecutionResult(
                ok=True,
                status="completed",
                message="用户今天已经签到。",
                output={
                    "provider_status": "already_signed",
                    "user_id": sender_id,
                    "group_id": room_id,
                    "total_sign_count": total_count,
                    "points": current_points,
                    "reply_text": reply_text,
                    "replies": [{"type": "text", "content": reply_text}],
                },
            )

        previous_sign = _normalize_dt(record.sign_stat, now.tzinfo) if record and record.sign_stat else None
        previous_streak = int(record.signin_streak or 0) if record else 0
        streak_broken = bool(previous_sign and previous_sign < yesterday_start)
        streak = previous_streak + 1 if previous_sign and previous_sign >= yesterday_start else 1
        points_awarded = _calculate_points(config, streak)
        todays_rank = _count_signs_for_day(db, room_id, today_start, today_start + timedelta(days=1)) + 1

        if record is None:
            record = SignInRecord(
                user_id=sender_id,
                group_id=room_id,
                user_name=sender_name,
                points=0,
                total_sign_count=0,
                signin_streak=0,
                previous_streak=0,
            )
            db.add(record)
            db.flush()

        record.user_name = sender_name
        record.last_sign_date = record.sign_stat
        record.sign_stat = now
        record.signin_streak = streak
        record.previous_streak = previous_streak if streak_broken and previous_streak > 1 else 0
        record.total_sign_count = int(record.total_sign_count or 0) + 1
        record.points = int(record.points or 0) + points_awarded

        history = SignInHistory(
            record_id=record.id,
            user_id=sender_id,
            group_id=room_id,
            signed_at=now,
            is_makeup=False,
            points_delta=points_awarded,
            streak=streak,
            note="sign",
        )
        db.add(history)
        points_result = points_service.add_points(
            sender_id,
            room_id,
            points_awarded,
            user_name=sender_name,
            source="checkin",
            description="签到奖励",
        )
        if not points_result.ok:
            db.rollback()
            return PluginExecutionResult(ok=False, status="error", message=points_result.message)
        db.commit()
        balance = int((points_result.account or {}).get("total_points") or record.points)

        lines = [f"签到成功，获得 {points_awarded} 积分，今天第 {todays_rank} 个签到。"]
        if streak_broken and previous_streak > 1:
            lines.append(f"此前连续签到 {previous_streak} 天，已重新开始累计。")
        elif streak > 1:
            lines.append(f"当前连续签到 {streak} 天。")
        lines.append(f"累计签到 {record.total_sign_count} 次，当前积分 {balance}。")
        reply_text = "\n".join(lines)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="签到成功。",
            output={
                "provider_status": "signed",
                "user_id": sender_id,
                "group_id": room_id,
                "points_awarded": points_awarded,
                "points": balance,
                "signin_streak": streak,
                "total_sign_count": record.total_sign_count,
                "todays_rank": todays_rank,
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _handle_makeup_sign(request: PluginExecutionRequest, config: SignConfig) -> PluginExecutionResult:
    now = _now(config.timezone)
    sender_id = _resolve_sender_id(request.payload)
    if not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="补签缺少 sender_id/user。")
    room_id = _resolve_room_id(request.payload)
    sender_name = _resolve_sender_name(request.payload, sender_id)
    today_start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    yesterday_start = today_start - timedelta(days=1)
    day_before_yesterday = yesterday_start - timedelta(days=1)

    with SessionLocal() as db:
        points_service = PointsService(db)
        record = db.scalar(select(SignInRecord).where(SignInRecord.user_id == sender_id, SignInRecord.group_id == room_id))
        if record is None:
            return PluginExecutionResult(ok=False, status="error", message="还没有签到记录，无法补签。")

        current_sign = _normalize_dt(record.sign_stat, now.tzinfo) if record.sign_stat else None
        last_sign = _normalize_dt(record.last_sign_date, now.tzinfo) if record.last_sign_date else None
        if current_sign and current_sign < today_start:
            last_sign = current_sign

        can_makeup = last_sign is not None and day_before_yesterday <= last_sign < yesterday_start
        if not can_makeup:
            return PluginExecutionResult(ok=False, status="error", message="当前只支持补签昨天，且仅限断签一天内。")
        account = points_service.get_user_points(sender_id, room_id) or {}
        current_points = int(account.get("total_points") or record.points or 0)
        if current_points < config.makeup_cost:
            return PluginExecutionResult(
                ok=False,
                status="error",
                message=f"积分不足，补签需要 {config.makeup_cost} 积分，当前仅有 {current_points} 积分。",
            )

        previous_streak = int(record.previous_streak or 0)
        if previous_streak > 0:
            new_streak = previous_streak + 1
        else:
            new_streak = int(record.signin_streak or 0) + 1

        record.user_name = sender_name
        record.points = int(record.points or 0) - config.makeup_cost
        record.signin_streak = new_streak
        record.previous_streak = 0
        if current_sign and current_sign >= today_start:
            record.last_sign_date = yesterday_start
        else:
            record.last_sign_date = last_sign
            record.sign_stat = yesterday_start

        history = SignInHistory(
            record_id=record.id,
            user_id=sender_id,
            group_id=room_id,
            signed_at=yesterday_start,
            is_makeup=True,
            points_delta=-config.makeup_cost,
            streak=new_streak,
            note="makeup_sign",
        )
        db.add(history)
        deduct_result = points_service.deduct_points(
            sender_id,
            room_id,
            config.makeup_cost,
            user_name=sender_name,
            source="checkin_makeup",
            description="补签消耗",
        )
        if not deduct_result.ok:
            db.rollback()
            return PluginExecutionResult(ok=False, status="error", message=deduct_result.message)
        db.commit()
        balance = int((deduct_result.account or {}).get("total_points") or record.points)

        reply_text = (
            f"补签成功，消耗 {config.makeup_cost} 积分。"
            f"\n连续签到恢复为 {new_streak} 天。"
            f"\n当前积分 {balance}。"
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="补签成功。",
            output={
                "provider_status": "makeup_signed",
                "user_id": sender_id,
                "group_id": room_id,
                "signin_streak": new_streak,
                "points": balance,
                "cost": config.makeup_cost,
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _load_config(request: PluginExecutionRequest) -> SignConfig:
    return SignConfig(
        enabled=bool(request.config.get("enable", True)),
        timezone=str(request.config.get("timezone") or "Asia/Shanghai").strip() or "Asia/Shanghai",
        min_point=max(_coerce_int(request.config.get("min_point"), 3), 1),
        max_point=max(_coerce_int(request.config.get("max_point"), 50), 1),
        streak_cycle=max(_coerce_int(request.config.get("streak_cycle"), 1), 1),
        max_streak_point=max(_coerce_int(request.config.get("max_streak_point"), 50), 0),
        makeup_cost=max(_coerce_int(request.config.get("makeup_cost"), 10), 0),
        sign_commands=_normalize_commands(request.config.get("command"), DEFAULT_SIGN_COMMANDS),
        makeup_commands=_normalize_commands(request.config.get("makeup_command"), DEFAULT_MAKEUP_COMMANDS),
    )


def _resolve_command(payload: dict[str, object]) -> str:
    for key in ("command_name", "command", "matched_keyword"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    content = str(payload.get("content") or "").strip()
    return content.split(" ", 1)[0] if content else ""


def _resolve_sender_id(payload: dict[str, object]) -> str:
    return str(payload.get("sender_id") or payload.get("user") or "").strip()


def _resolve_sender_name(payload: dict[str, object], fallback: str) -> str:
    return str(payload.get("sender_name") or payload.get("user_name") or fallback).strip() or fallback


def _resolve_room_id(payload: dict[str, object]) -> str:
    if str(payload.get("chat_type") or "").strip() == "group":
        return str(payload.get("conversation_external_id") or payload.get("room_id") or "").strip()
    return str(payload.get("room_id") or "").strip()


def _normalize_commands(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list):
        commands = tuple(str(item).strip() for item in value if str(item).strip())
        return commands or default
    if isinstance(value, str):
        commands = tuple(item.strip() for item in value.split(",") if item.strip())
        return commands or default
    return default


def _calculate_points(config: SignConfig, streak: int) -> int:
    extra = min(streak // config.streak_cycle, config.max_streak_point)
    return min(config.min_point + extra, config.max_point)


def _count_signs_for_day(db: Session, group_id: str, start: datetime, end: datetime) -> int:
    stmt = select(func.count(SignInHistory.id)).where(
        SignInHistory.group_id == group_id,
        SignInHistory.is_makeup.is_(False),
        SignInHistory.signed_at >= start,
        SignInHistory.signed_at < end,
    )
    count = db.scalar(stmt)
    return int(count or 0)


def _normalize_dt(value: datetime | None, tzinfo: timezone | ZoneInfo) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=tzinfo)
    return value.astimezone(tzinfo)


def _now(timezone_name: str) -> datetime:
    return datetime.now(_resolve_tzinfo(timezone_name))


def _resolve_tzinfo(timezone_name: str) -> timezone | ZoneInfo:
    normalized = timezone_name.strip()
    if not normalized:
        return now_shanghai().tzinfo or timezone(timedelta(hours=8))
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        if normalized == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        return now_shanghai().tzinfo or timezone(timedelta(hours=8))


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
