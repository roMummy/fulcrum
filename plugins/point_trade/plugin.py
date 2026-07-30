from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from app.core.time import now_shanghai
from app.core.db import SessionLocal
from app.services.points import BAILOUT_COST, SYSTEM_USER_ID, PointsService
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_COMMANDS = (
    "积分交易",
    "积分转账",
    "转账积分",
    "积分赠送",
    "赠送积分",
    "积分转移",
    "转移积分",
    "送积分",
    "积分送人",
    "送人积分",
    "积分赠予",
    "我的积分",
    "points_me",
    "积分排行",
    "points_rank",
    "打劫",
    "rob",
    "保释",
    "bailout",
    "point_trade",
)


@dataclass(frozen=True)
class PointTradeConfig:
    enabled: bool
    commands: tuple[str, ...]
    rob_success_rate: float
    rob_min_percent: float
    rob_max_percent: float
    rob_penalty_percent: float
    rob_min_points: int
    rob_cooldown: int


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="point_trade runtime is ready",
            output={
                "enabled": config.enabled,
                "commands": list(config.commands),
                "rob_success_rate": config.rob_success_rate,
                "rob_cooldown": config.rob_cooldown,
                "rob_min_points": config.rob_min_points,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not config.enabled:
        return PluginExecutionResult(ok=False, status="disabled", message="积分交易插件当前未启用。")

    command = _resolve_command(request.payload)
    if command in {"我的积分", "points_me"}:
        return _handle_my_points(request)
    if command in {"积分排行", "points_rank"}:
        return _handle_ranking(request)
    if command in {"打劫", "rob"}:
        return _handle_rob(request, config)
    if command in {"保释", "bailout"}:
        return _handle_bailout(request)
    return _handle_transfer(request)


def _handle_transfer(request: PluginExecutionRequest) -> PluginExecutionResult:
    sender_id = _resolve_sender_id(request.payload)
    if not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="积分转账缺少 sender_id/user。")
    group_id = _resolve_group_id(request.payload)
    if not group_id:
        return PluginExecutionResult(ok=False, status="error", message="积分转账仅支持群聊。")

    amount = _resolve_amount(request.payload)
    if amount <= 0:
        return PluginExecutionResult(ok=False, status="error", message="转账积分必须是大于 0 的整数。")
    target_user_id = _resolve_target_user_id(request.payload)
    if not target_user_id:
        return PluginExecutionResult(ok=False, status="error", message="未识别转账目标，请传入 target_user_id 或在命令里写明目标。")

    sender_name = _resolve_sender_name(request.payload, sender_id)
    target_user_name = _resolve_target_user_name(request.payload, target_user_id)
    with SessionLocal() as db:
        points_service = PointsService(db)
        result = points_service.transfer_points(
            sender_id,
            target_user_id,
            group_id,
            amount,
            from_user_name=sender_name,
            to_user_name=target_user_name,
            description="积分转账",
            source="trade",
        )
        if not result.ok:
            return PluginExecutionResult(ok=False, status="error", message=result.message)
        extra = result.extra or {}
        from_user = extra.get("from_user") if isinstance(extra.get("from_user"), dict) else {}
        to_user = extra.get("to_user") if isinstance(extra.get("to_user"), dict) else {}
        reply_text = (
            f"积分转账成功。"
            f"\n{sender_name} 转给 {target_user_name} {amount} 积分。"
            f"\n{sender_name} 当前积分 {int(from_user.get('total_points') or 0)}。"
            f"\n{target_user_name} 当前积分 {int(to_user.get('total_points') or 0)}。"
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="积分转账成功。",
            output={
                "provider_status": "transfer_completed",
                "from_user": from_user,
                "to_user": to_user,
                "amount": amount,
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _handle_my_points(request: PluginExecutionRequest) -> PluginExecutionResult:
    sender_id = _resolve_sender_id(request.payload)
    if not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="查询积分缺少 sender_id/user。")
    group_id = _resolve_group_id(request.payload)
    if not group_id:
        return PluginExecutionResult(ok=False, status="error", message="查询积分仅支持群聊。")

    sender_name = _resolve_sender_name(request.payload, sender_id)
    with SessionLocal() as db:
        points_service = PointsService(db)
        account = points_service.get_user_points(sender_id, group_id)
        if account is None:
            return PluginExecutionResult(ok=False, status="error", message="未找到积分记录，请先通过签到等活动获取积分。")
        transactions = points_service.get_user_transactions(sender_id, group_id, 5)
        lines = [f"{sender_name} 当前积分 {int(account.get('total_points') or 0)}。"]
        if transactions:
            lines.append("最近流水：")
            for item in transactions:
                delta = int(item.get("points") or 0)
                sign = "+" if delta > 0 else ""
                description = str(item.get("description") or item.get("source") or "积分变动")
                lines.append(f"{sign}{delta} {description}")
        reply_text = "\n".join(lines)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="积分查询成功。",
            output={
                "provider_status": "points_detail",
                "account": account,
                "transactions": transactions,
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _handle_ranking(request: PluginExecutionRequest) -> PluginExecutionResult:
    group_id = _resolve_group_id(request.payload)
    if not group_id:
        return PluginExecutionResult(ok=False, status="error", message="积分排行仅支持群聊。")

    with SessionLocal() as db:
        points_service = PointsService(db)
        ranking = points_service.get_points_ranking(group_id, 10)
        if not ranking:
            return PluginExecutionResult(ok=False, status="error", message="当前群还没有积分排行数据。")
        lines = ["积分排行榜："]
        for index, item in enumerate(ranking, start=1):
            name = str(item.get("user_name") or item.get("user_id") or f"用户{index}")
            lines.append(f"{index}. {name} {int(item.get('total_points') or 0)} 积分")
        reply_text = "\n".join(lines)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="积分排行查询成功。",
            output={
                "provider_status": "ranking",
                "ranking": ranking,
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _handle_rob(request: PluginExecutionRequest, config: PointTradeConfig) -> PluginExecutionResult:
    sender_id = _resolve_sender_id(request.payload)
    group_id = _resolve_group_id(request.payload)
    if not sender_id or not group_id:
        return PluginExecutionResult(ok=False, status="error", message="打劫仅支持群聊且需要 sender_id。")
    target_user_id = _resolve_target_user_id(request.payload)
    if not target_user_id:
        return PluginExecutionResult(ok=False, status="error", message="未识别打劫目标，请传入 target_user_id。")
    if target_user_id == sender_id:
        return PluginExecutionResult(ok=False, status="error", message="不能打劫自己。")

    sender_name = _resolve_sender_name(request.payload, sender_id)
    target_name = _resolve_target_user_name(request.payload, target_user_id)
    with SessionLocal() as db:
        points_service = PointsService(db)
        prison_status = points_service.check_prison_status(sender_id, group_id)
        if prison_status:
            end_at = prison_status.get("end_at")
            remaining = ""
            if isinstance(end_at, datetime):
                seconds = max(int((end_at - now_shanghai()).total_seconds()), 0)
                remaining = f"{seconds // 3600}小时{(seconds % 3600) // 60}分钟"
            return PluginExecutionResult(ok=False, status="error", message=f"你正在服刑，剩余时间 {remaining or '未到释放时间'}。")

        robber = points_service.get_user_points(sender_id, group_id)
        target = points_service.get_user_points(target_user_id, group_id)
        if robber is None or target is None:
            return PluginExecutionResult(ok=False, status="error", message="双方都需要先拥有积分记录。")
        robber_points = int(robber.get("total_points") or 0)
        target_points = int(target.get("total_points") or 0)
        if robber_points < config.rob_min_points:
            return PluginExecutionResult(ok=False, status="error", message=f"你的积分不足 {config.rob_min_points}，无法打劫。")
        if target_points < config.rob_min_points:
            return PluginExecutionResult(ok=False, status="error", message=f"目标积分不足 {config.rob_min_points}，不值得打劫。")

        if random.random() < config.rob_success_rate:
            rob_percent = random.uniform(config.rob_min_percent, config.rob_max_percent)
            rob_amount = max(1, int(target_points * rob_percent))
            tax_amount = max(1, int(rob_amount * 0.2))
            actual_amount = max(1, rob_amount - tax_amount)
            transfer_result = points_service.transfer_points(
                target_user_id,
                sender_id,
                group_id,
                actual_amount,
                from_user_name=target_name,
                to_user_name=sender_name,
                description="打劫成功",
                source="rob",
            )
            if not transfer_result.ok:
                return PluginExecutionResult(ok=False, status="error", message=transfer_result.message)
            tax_result = points_service.transfer_points(
                target_user_id,
                SYSTEM_USER_ID,
                group_id,
                tax_amount,
                from_user_name=target_name,
                to_user_name="系统",
                description="打劫抽水",
                source="rob_tax",
            )
            if not tax_result.ok:
                return PluginExecutionResult(ok=False, status="error", message=tax_result.message)
            extra = transfer_result.extra or {}
            reply_text = (
                f"打劫成功。"
                f"\n{sender_name} 从 {target_name} 身上抢到 {rob_amount} 积分。"
                f"\n实际到手 {actual_amount}，系统抽水 {tax_amount}。"
                f"\n你当前积分 {int(((extra.get('to_user') or {}) if isinstance(extra.get('to_user'), dict) else {}).get('total_points') or 0)}。"
            )
            return PluginExecutionResult(
                ok=True,
                status="completed",
                message="打劫成功。",
                output={
                    "provider_status": "rob_success",
                    "amount": actual_amount,
                    "tax_amount": tax_amount,
                    "reply_text": reply_text,
                    "replies": [{"type": "text", "content": reply_text}],
                },
            )

        penalty_amount = max(1, int(robber_points * config.rob_penalty_percent))
        penalty_result = points_service.transfer_points(
            sender_id,
            target_user_id,
            group_id,
            penalty_amount,
            from_user_name=sender_name,
            to_user_name=target_name,
            description="打劫失败罚款",
            source="rob_penalty",
        )
        if not penalty_result.ok:
            return PluginExecutionResult(ok=False, status="error", message=penalty_result.message)
        points_service.imprison_user(sender_id, group_id, 24, reason=f"打劫 {target_user_id} 失败")
        reply_text = (
            f"打劫失败。"
            f"\n你被罚 {penalty_amount} 积分，并被关押 24 小时。"
            f"\n可请求他人花费 {BAILOUT_COST} 积分保释。"
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="打劫失败。",
            output={
                "provider_status": "rob_failed",
                "penalty_amount": penalty_amount,
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _handle_bailout(request: PluginExecutionRequest) -> PluginExecutionResult:
    sender_id = _resolve_sender_id(request.payload)
    group_id = _resolve_group_id(request.payload)
    if not sender_id or not group_id:
        return PluginExecutionResult(ok=False, status="error", message="保释仅支持群聊且需要 sender_id。")
    target_user_id = _resolve_target_user_id(request.payload)
    if not target_user_id:
        return PluginExecutionResult(ok=False, status="error", message="未识别保释目标，请传入 target_user_id。")
    if target_user_id == sender_id:
        return PluginExecutionResult(ok=False, status="error", message="不能保释自己。")

    sender_name = _resolve_sender_name(request.payload, sender_id)
    target_name = _resolve_target_user_name(request.payload, target_user_id)
    with SessionLocal() as db:
        points_service = PointsService(db)
        result = points_service.bailout_user(target_user_id, sender_id, group_id)
        if not result.ok:
            return PluginExecutionResult(ok=False, status="error", message=result.message)
        account = points_service.get_user_points(sender_id, group_id) or {}
        reply_text = (
            f"保释成功。"
            f"\n{sender_name} 花费 {BAILOUT_COST} 积分保释了 {target_name}。"
            f"\n你当前积分 {int(account.get('total_points') or 0)}。"
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="保释成功。",
            output={
                "provider_status": "bailout_success",
                "cost": BAILOUT_COST,
                "points": int(account.get("total_points") or 0),
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _load_config(request: PluginExecutionRequest) -> PointTradeConfig:
    return PointTradeConfig(
        enabled=bool(request.config.get("enable", True)),
        commands=_normalize_commands(request.config.get("command"), DEFAULT_COMMANDS),
        rob_success_rate=_coerce_float(request.config.get("rob_success_rate"), 0.4),
        rob_min_percent=_coerce_float(request.config.get("rob_min_percent"), 0.2),
        rob_max_percent=_coerce_float(request.config.get("rob_max_percent"), 0.4),
        rob_penalty_percent=_coerce_float(request.config.get("rob_penalty_percent"), 0.1),
        rob_min_points=max(_coerce_int(request.config.get("rob_min_points"), 30), 1),
        rob_cooldown=max(_coerce_int(request.config.get("rob_cooldown"), 300), 0),
    )


def _normalize_commands(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list):
        result = tuple(str(item).strip() for item in value if str(item).strip())
        return result or default
    if isinstance(value, str):
        result = tuple(item.strip() for item in value.split(",") if item.strip())
        return result or default
    return default


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


def _resolve_target_user_name(payload: dict[str, object], fallback: str) -> str:
    return str(payload.get("target_user_name") or payload.get("target_name") or fallback).strip() or fallback


def _resolve_group_id(payload: dict[str, object]) -> str:
    if str(payload.get("chat_type") or "").strip() != "group":
        return ""
    return str(payload.get("conversation_external_id") or payload.get("room_id") or "").strip()


def _resolve_amount(payload: dict[str, object]) -> int:
    for key in ("amount", "points", "value"):
        value = _coerce_int(payload.get(key), 0)
        if value > 0:
            return value
    args = str(payload.get("command_args") or payload.get("stripped_content") or "").strip().split()
    for token in args:
        if token.isdigit():
            return int(token)
    return 0


def _resolve_target_user_id(payload: dict[str, object]) -> str:
    for key in ("target_user_id", "target_id", "recipient_id", "mentioned_user_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    args = str(payload.get("command_args") or payload.get("stripped_content") or "").strip().split()
    for token in reversed(args):
        normalized = token.strip().lstrip("@")
        if normalized and not normalized.isdigit():
            return normalized
    return ""


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
