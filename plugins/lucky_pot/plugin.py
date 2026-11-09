from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Conversation, Message
from app.services.outbound_batch import enqueue_outbound_jobs
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.scheduler import enqueue_plugin_scheduled_action
from app.services.plugin_runtime.state_store import PluginStateStore


COMMAND = "大奖池"
STATE_NAMESPACE = "lucky_pot"
STATE_KEY_PREFIX = "conversation"
DRAW_ACTION = "draw_pot"
MAX_PLAYERS_LIMIT = 50


class LuckyPotStateError(ValueError):
    """奖池状态结构损坏或超过当前产品容量。"""


@dataclass(frozen=True)
class LuckyPotConfig:
    """当前绑定的奖池规则。"""

    entry_fee: int
    min_players: int
    max_players: int
    countdown_seconds: int
    house_cut_percent: int
    lucky_bonus_chance: int
    lucky_bonus_percent: int


@dataclass(frozen=True)
class TrustedGroupMessage:
    """由数据库交叉验证后的群、绑定和消息定位。"""

    context: PluginExecutionContext
    binding: BotPlugin
    conversation: Conversation
    message: Message
    sender_id: str
    sender_name: str


@dataclass(frozen=True)
class SettlementOutcome:
    """退款或开奖后的可公开业务结果。"""

    provider_status: str
    message: str
    reply_text: str
    output: Mapping[str, Any]


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供重载检查，积分奖池必须绑定真实群消息。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="幸运池插件已就绪。",
            output={
                "entry_fee": config.entry_fee,
                "min_players": config.min_players,
                "max_players": config.max_players,
                "countdown_seconds": config.countdown_seconds,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("大奖池只能通过真实群聊消息操作。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在可信群会话内创建、参与、查看、开奖或取消唯一活动奖池。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    trusted, error = _require_group_message_context(db, request.context)
    if error is not None or trusted is None:
        return error or _error_result("大奖池缺少可信群聊消息。", "trusted_context_required")

    subcommand = _parse_subcommand(trusted.message.content)
    store = PluginStateStore(db, trusted.context)
    state_key = _state_key(trusted.context)
    try:
        room = _load_room(store, state_key)
    except LuckyPotStateError:
        return _error_result("当前群的大奖池状态异常，请联系管理员重置插件状态。", "state_invalid")

    if subcommand == "创建":
        return _create_room(db, store, state_key, trusted, room, config)
    if subcommand == "参与":
        return _join_room(db, store, state_key, trusted, room)
    if subcommand == "查看":
        return _view_room(room)
    if subcommand == "取消":
        return _cancel_room(db, store, state_key, trusted, room)
    if subcommand == "开奖":
        return _draw_room(db, store, state_key, trusted, room)
    return _help_result(config)


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """倒计时到期后开奖；人数不足时自动退款并通过统一出站队列通知群聊。"""

    if request.action != DRAW_ACTION:
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的幸运池定时动作: {request.action}",
        )
    context, binding, conversation, channel, error = _require_scheduled_group_context(db, request.context)
    if error is not None or context is None or binding is None or conversation is None or channel is None:
        return error or _error_result("幸运池定时任务缺少可信群聊。", "trusted_context_required")
    store = PluginStateStore(db, context)
    state_key = _state_key(context)
    try:
        room = _load_room(store, state_key)
    except LuckyPotStateError:
        return _error_result("当前群的大奖池状态异常，无法结算。", "state_invalid")
    if room is None:
        return PluginExecutionResult(
            ok=True,
            status="skipped",
            message="当前群已没有待结算大奖池。",
            output={"provider_status": "room_missing"},
        )

    outcome = _settle_room(db, store, state_key, room)
    job_ids = enqueue_outbound_jobs(
        db,
        conversation=conversation,
        channel=channel,
        payloads=[_text_outbound_payload(conversation, outcome.reply_text)],
        plugin_code="lucky_pot",
        related_type="lucky_pot_binding",
        related_id=int(binding.id),
        priority=65,
    )
    db.commit()
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=outcome.message,
        output={
            "provider_status": outcome.provider_status,
            **dict(outcome.output),
            "conversation_id": int(conversation.id),
            "outbound_job_ids": [int(item) for item in job_ids],
        },
    )


def _create_room(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    trusted: TrustedGroupMessage,
    room: dict[str, Any] | None,
    config: LuckyPotConfig,
) -> PluginExecutionResult:
    """创建者先支付入场费，再为当前群建立唯一房间和倒计时任务。"""

    if room is not None:
        return _error_result("当前群已有进行中的大奖池，请先查看或完成本轮。", "room_exists")
    group_id = str(trusted.conversation.external_id or "").strip()
    points_service = PointsService(db)
    current_points = points_service.balance(trusted.context)
    if current_points < config.entry_fee:
        return _error_result(
            f"创建大奖池需要 {config.entry_fee} 积分，当前仅有 {current_points}。",
            "insufficient_points",
        )

    room_id = _stable_room_id(trusted.context)
    draw_at = now_shanghai() + timedelta(seconds=config.countdown_seconds)
    room = {
        "room_id": room_id,
        "status": "waiting",
        "creator_id": trusted.sender_id,
        "creator_name": trusted.sender_name,
        "group_id": group_id,
        "entry_fee": config.entry_fee,
        "min_players": config.min_players,
        "max_players": config.max_players,
        "house_cut_percent": config.house_cut_percent,
        "lucky_bonus_chance": config.lucky_bonus_chance,
        "lucky_bonus_percent": config.lucky_bonus_percent,
        "players": {trusted.sender_id: trusted.sender_name},
        "created_at": now_shanghai().isoformat(),
        "draw_at": draw_at.isoformat(),
    }
    # 房间状态先进入当前事务，再由积分服务提交扣款。这样不会出现扣款成功但房间
    # 或倒计时任务缺失的中间状态；扣款失败时显式回滚全部待写入对象。
    _save_room(store, state_key, room)
    job, _created = enqueue_plugin_scheduled_action(
        db,
        trusted.binding,
        action_key=DRAW_ACTION,
        scheduled_for=draw_at,
        conversation_id=int(trusted.conversation.id),
        # 所有退款和奖励都有幂等键，允许 Worker 在中断后重试完成剩余结算。
        high_side_effect=False,
        priority=65,
    )
    deducted = points_service.spend(
        trusted.context,
        config.entry_fee,
        source="lucky_pot_entry",
        description=f"大奖池 {room_id} 入场费",
        metadata={"conversation_id": int(trusted.conversation.id), "room_id": room_id},
        idempotency_key=_entry_key(trusted.context, room_id, trusted.sender_id),
    )
    if not deducted.ok:
        db.rollback()
        return _error_result(deducted.message, "points_failed")
    balance = int((deducted.account or {}).get("total_points") or 0)
    reply_text = (
        f"大奖池已创建，房间号 {room_id}。\n"
        f"入场费：{config.entry_fee} 积分\n"
        f"人数：1/{config.max_players}，至少 {config.min_players} 人开奖\n"
        f"倒计时：{config.countdown_seconds} 秒\n"
        f"发送“大奖池 参与”加入，发送“大奖池 查看”查看状态。"
    )
    return _text_result(
        reply_text,
        "created",
        room_id=room_id,
        player_count=1,
        current_points=balance,
        scheduled_job_id=int(job.id),
    )


def _join_room(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    trusted: TrustedGroupMessage,
    room: dict[str, Any] | None,
) -> PluginExecutionResult:
    """参与者支付固定入场费；满员后在同一业务路径立即开奖。"""

    if room is None or room.get("status") != "waiting":
        return _error_result("当前没有可参与的大奖池。", "room_missing")
    players = _room_players(room)
    if trusted.sender_id in players:
        return _error_result("你已经参与当前大奖池。", "already_joined")
    max_players = int(room["max_players"])
    if len(players) >= max_players:
        return _error_result("当前大奖池人数已满。", "room_full")

    entry_fee = int(room["entry_fee"])
    points_service = PointsService(db)
    current_points = points_service.balance(trusted.context)
    if current_points < entry_fee:
        return _error_result(
            f"参与大奖池需要 {entry_fee} 积分，当前仅有 {current_points}。",
            "insufficient_points",
        )

    players[trusted.sender_id] = trusted.sender_name
    room["players"] = players
    _save_room(store, state_key, room)
    deducted = points_service.spend(
        trusted.context,
        entry_fee,
        source="lucky_pot_entry",
        description=f"大奖池 {room['room_id']} 入场费",
        metadata={"conversation_id": int(trusted.conversation.id), "room_id": str(room["room_id"])},
        idempotency_key=_entry_key(trusted.context, str(room["room_id"]), trusted.sender_id),
    )
    if not deducted.ok:
        db.rollback()
        return _error_result(deducted.message, "points_failed")

    if len(players) >= max_players:
        outcome = _settle_room(db, store, state_key, room)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=outcome.message,
            output={
                "provider_status": outcome.provider_status,
                **dict(outcome.output),
                "replies": standard_replies([text_reply(outcome.reply_text)]),
            },
        )

    balance = int((deducted.account or {}).get("total_points") or 0)
    reply_text = (
        f"{trusted.sender_name} 已参与大奖池。"
        f"当前 {len(players)}/{max_players} 人，奖池 {entry_fee * len(players)} 积分。"
    )
    return _text_result(
        reply_text,
        "joined",
        room_id=str(room["room_id"]),
        player_count=len(players),
        total_pot=entry_fee * len(players),
        current_points=balance,
    )


def _view_room(room: dict[str, Any] | None) -> PluginExecutionResult:
    if room is None:
        return _error_result("当前群没有进行中的大奖池。", "room_missing")
    players = _room_players(room)
    status = str(room.get("status") or "waiting")
    lines = [
        f"大奖池状态：{_status_text(status)}",
        f"房间号：{room['room_id']}",
        f"人数：{len(players)}/{room['max_players']}",
        f"奖池：{int(room['entry_fee']) * len(players)} 积分",
        "参与者：" + "、".join(players.values()),
    ]
    return _text_result(
        "\n".join(lines),
        "viewed",
        room_id=str(room["room_id"]),
        room_status=status,
        player_count=len(players),
    )


def _cancel_room(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    trusted: TrustedGroupMessage,
    room: dict[str, Any] | None,
) -> PluginExecutionResult:
    if room is None:
        return _error_result("当前群没有可取消的大奖池。", "room_missing")
    if trusted.sender_id != str(room.get("creator_id") or ""):
        return _error_result("只有创建者可以取消大奖池。", "forbidden")
    if str(room.get("status") or "") not in {"waiting", "refunding"}:
        return _error_result("当前大奖池已经进入开奖，不能取消。", "room_settling")
    outcome = _refund_room(db, store, state_key, room, reason="创建者取消")
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=outcome.message,
        output={
            "provider_status": outcome.provider_status,
            **dict(outcome.output),
            "replies": standard_replies([text_reply(outcome.reply_text)]),
        },
    )


def _draw_room(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    trusted: TrustedGroupMessage,
    room: dict[str, Any] | None,
) -> PluginExecutionResult:
    if room is None:
        return _error_result("当前群没有可开奖的大奖池。", "room_missing")
    if trusted.sender_id != str(room.get("creator_id") or ""):
        return _error_result("只有创建者可以提前开奖。", "forbidden")
    if len(_room_players(room)) < int(room["min_players"]):
        return _error_result(
            f"至少需要 {room['min_players']} 人才能开奖。",
            "insufficient_players",
        )
    outcome = _settle_room(db, store, state_key, room)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=outcome.message,
        output={
            "provider_status": outcome.provider_status,
            **dict(outcome.output),
            "replies": standard_replies([text_reply(outcome.reply_text)]),
        },
    )


def _settle_room(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    room: dict[str, Any],
) -> SettlementOutcome:
    """人数不足则退款，否则使用已持久化开奖计划完成可重试派奖。"""

    status = str(room.get("status") or "waiting")
    if status == "refunding":
        return _refund_room(
            db,
            store,
            state_key,
            room,
            reason=str(room.get("refund_reason") or "奖池取消"),
        )
    if status == "drawing":
        return _award_room(db, store, state_key, room)
    players = _room_players(room)
    if len(players) < int(room["min_players"]):
        return _refund_room(db, store, state_key, room, reason="倒计时结束时人数不足")
    return _award_room(db, store, state_key, room)


def _refund_room(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    room: dict[str, Any],
    *,
    reason: str,
) -> SettlementOutcome:
    """先标记退款中，再按玩家幂等返还入场费，最后删除活动状态。"""

    room["status"] = "refunding"
    room["refund_reason"] = reason
    _save_room(store, state_key, room)
    room_id = str(room["room_id"])
    entry_fee = int(room["entry_fee"])
    group_id = str(room["group_id"])
    players = _room_players(room)
    service = PointsService(db)
    for user_id, user_name in players.items():
        result = service.add_points(
            user_id,
            group_id,
            entry_fee,
            user_name=user_name,
            source="lucky_pot_refund",
            description=f"大奖池 {room_id} 退款",
            metadata={"room_id": room_id, "reason": reason},
            idempotency_key=f"lucky-pot:{room_id}:refund:{user_id}",
        )
        if not result.ok:
            raise LuckyPotStateError("大奖池退款失败")
    store.delete(STATE_NAMESPACE, state_key)
    db.commit()
    reply_text = f"大奖池已取消：{reason}。已向 {len(players)} 位参与者退还全部入场积分。"
    return SettlementOutcome(
        provider_status="refunded",
        message="大奖池已取消并退款。",
        reply_text=reply_text,
        output={"room_id": room_id, "refunded_player_count": len(players)},
    )


def _award_room(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    room: dict[str, Any],
) -> SettlementOutcome:
    """首次开奖先保存随机分配方案，重试时复用同一方案并依靠积分键去重。"""

    settlement = room.get("settlement")
    if str(room.get("status") or "") != "drawing" or not isinstance(settlement, Mapping):
        settlement = _build_settlement(room)
        room["status"] = "drawing"
        room["settlement"] = dict(settlement)
        _save_room(store, state_key, room)
        db.flush()

    room_id = str(room["room_id"])
    group_id = str(room["group_id"])
    players = _room_players(room)
    rewards_raw = settlement.get("rewards")
    if not isinstance(rewards_raw, Mapping):
        raise LuckyPotStateError("大奖池开奖方案缺少奖励")
    rewards = {str(key): int(value) for key, value in rewards_raw.items()}
    service = PointsService(db)
    for user_id, user_name in players.items():
        reward = int(rewards.get(user_id, 0))
        if reward <= 0:
            continue
        result = service.add_points(
            user_id,
            group_id,
            reward,
            user_name=user_name,
            source="lucky_pot_reward",
            description=f"大奖池 {room_id} 开奖奖励",
            metadata={"room_id": room_id},
            idempotency_key=f"lucky-pot:{room_id}:reward:{user_id}",
        )
        if not result.ok:
            raise LuckyPotStateError("大奖池奖励派发失败")

    store.delete(STATE_NAMESPACE, state_key)
    db.commit()
    ranking = sorted(players.items(), key=lambda item: (-rewards.get(item[0], 0), item[1], item[0]))
    total_pot = int(settlement["total_pot"])
    house_cut = int(settlement["house_cut"])
    lines = [f"大奖池 {room_id} 开奖完成。", f"总奖池 {total_pot} 积分，系统结算 {house_cut} 积分。"]
    for index, (user_id, user_name) in enumerate(ranking, start=1):
        reward = rewards.get(user_id, 0)
        profit = reward - int(room["entry_fee"])
        lines.append(f"{index}. {user_name} 获得 {reward} 积分，净收益 {profit:+d}")
    lucky_winner = str(settlement.get("lucky_winner") or "")
    lucky_bonus = int(settlement.get("lucky_bonus") or 0)
    if lucky_winner and lucky_bonus > 0:
        lines.append(f"幸运加成：{players.get(lucky_winner, lucky_winner)} 获得额外 {lucky_bonus} 积分。")
    return SettlementOutcome(
        provider_status="drawn",
        message="大奖池开奖完成。",
        reply_text="\n".join(lines),
        output={
            "room_id": room_id,
            "total_pot": total_pot,
            "house_cut": house_cut,
            "rewarded_player_count": len([value for value in rewards.values() if value > 0]),
        },
    )


def _build_settlement(room: Mapping[str, Any]) -> dict[str, Any]:
    """生成守恒的随机奖励方案，幸运加成从可分配奖池中预留而非额外增发。"""

    players = _room_players(room)
    player_ids = list(players)
    total_pot = int(room["entry_fee"]) * len(player_ids)
    maximum_cut = max(total_pot - len(player_ids), 0)
    house_cut = min(int(total_pot * int(room["house_cut_percent"]) / 100), maximum_cut)
    distributable = total_pot - house_cut
    lucky_winner = ""
    lucky_bonus = 0
    bonus_capacity = max(distributable - len(player_ids), 0)
    if (
        len(player_ids) > 1
        and bonus_capacity > 0
        and random.randint(1, 100) <= int(room["lucky_bonus_chance"])
    ):
        lucky_winner = random.choice(player_ids)
        requested_bonus = max(1, int(distributable * int(room["lucky_bonus_percent"]) / 100))
        lucky_bonus = min(requested_bonus, bonus_capacity)
    base_total = distributable - lucky_bonus
    rewards = _distribute_rewards(player_ids, base_total)
    if lucky_winner:
        rewards[lucky_winner] = rewards.get(lucky_winner, 0) + lucky_bonus
    if sum(rewards.values()) != distributable:
        raise LuckyPotStateError("大奖池奖励方案不守恒")
    return {
        "total_pot": total_pot,
        "house_cut": house_cut,
        "rewards": rewards,
        "lucky_winner": lucky_winner,
        "lucky_bonus": lucky_bonus,
    }


def _distribute_rewards(player_ids: list[str], total_points: int) -> dict[str, int]:
    """为每位玩家至少分配一分，再随机切分剩余积分。"""

    if not player_ids or total_points < len(player_ids):
        raise LuckyPotStateError("可分配积分不足")
    remaining = total_points - len(player_ids)
    rewards = {user_id: 1 for user_id in player_ids}
    for index, user_id in enumerate(player_ids[:-1]):
        share = random.randint(0, remaining)
        rewards[user_id] += share
        remaining -= share
    rewards[player_ids[-1]] += remaining
    items = list(rewards.values())
    random.shuffle(items)
    return dict(zip(player_ids, items))


def _load_room(store: PluginStateStore, state_key: str) -> dict[str, Any] | None:
    payload = store.get(STATE_NAMESPACE, state_key)
    if payload is None:
        return None
    required = {
        "room_id",
        "status",
        "group_id",
        "creator_id",
        "creator_name",
        "entry_fee",
        "min_players",
        "max_players",
        "house_cut_percent",
        "lucky_bonus_chance",
        "lucky_bonus_percent",
        "players",
        "created_at",
        "draw_at",
    }
    if not required.issubset(payload):
        raise LuckyPotStateError("大奖池状态字段不完整")
    players = _room_players(payload)
    if not players or len(players) > MAX_PLAYERS_LIMIT:
        raise LuckyPotStateError("大奖池玩家数量不合法")
    if str(payload.get("creator_id") or "") not in players:
        raise LuckyPotStateError("大奖池创建者不在玩家列表")
    entry_fee = _bounded_int(payload.get("entry_fee"), default=0, minimum=1, maximum=1_000_000)
    min_players = _bounded_int(payload.get("min_players"), default=0, minimum=2, maximum=MAX_PLAYERS_LIMIT)
    max_players = _bounded_int(payload.get("max_players"), default=0, minimum=2, maximum=MAX_PLAYERS_LIMIT)
    if min_players > max_players or len(players) > max_players:
        raise LuckyPotStateError("大奖池人数规则不合法")
    payload["entry_fee"] = entry_fee
    payload["min_players"] = min_players
    payload["max_players"] = max_players
    payload["house_cut_percent"] = _bounded_int(payload.get("house_cut_percent"), default=0, minimum=0, maximum=30)
    payload["lucky_bonus_chance"] = _bounded_int(payload.get("lucky_bonus_chance"), default=0, minimum=0, maximum=100)
    payload["lucky_bonus_percent"] = _bounded_int(payload.get("lucky_bonus_percent"), default=0, minimum=0, maximum=50)
    payload["players"] = players
    return payload


def _save_room(store: PluginStateStore, state_key: str, room: Mapping[str, Any]) -> None:
    """状态键只使用可信会话主键，群外载荷不能选择或覆盖其他奖池。"""

    store.put(STATE_NAMESPACE, state_key, dict(room))


def _room_players(room: Mapping[str, Any]) -> dict[str, str]:
    raw = room.get("players")
    if not isinstance(raw, Mapping):
        raise LuckyPotStateError("大奖池玩家状态不是对象")
    players: dict[str, str] = {}
    for raw_user_id, raw_user_name in raw.items():
        user_id = _single_line(raw_user_id, limit=128)
        user_name = _single_line(raw_user_name, limit=64)
        if not user_id or not user_name:
            raise LuckyPotStateError("大奖池玩家信息不完整")
        players[user_id] = user_name
    return players


def _require_group_message_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedGroupMessage | None, PluginExecutionResult | None]:
    """交叉验证绑定、会话、消息和发送人，所有积分定位均来自数据库。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
    ):
        return None, _error_result("请在群聊中使用大奖池。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
        or str(conversation.chat_type or "") != "group"
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "") != "in"
        or not str(message.sender_id or "").strip()
        or (context.sender_id is not None and str(context.sender_id).strip() != str(message.sender_id).strip())
    ):
        return None, _error_result("当前群聊消息上下文无效。", "trusted_context_required")
    sender_id = str(message.sender_id or "").strip()
    sender_name = _single_line(message.sender_name or sender_id, limit=64) or sender_id
    return TrustedGroupMessage(
        context=context,
        binding=binding,
        conversation=conversation,
        message=message,
        sender_id=sender_id,
        sender_name=sender_name,
    ), None


def _require_scheduled_group_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[
    PluginExecutionContext | None,
    BotPlugin | None,
    Conversation | None,
    Channel | None,
    PluginExecutionResult | None,
]:
    if context is None or context.channel_id is None or context.conversation_id is None:
        return None, None, None, None, _error_result("幸运池定时任务缺少群聊。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    channel = db.get(Channel, int(context.channel_id))
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
        or str(conversation.chat_type or "") != "group"
        or channel is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or not channel.is_enabled
    ):
        return None, None, None, None, _error_result("幸运池定时任务上下文无效。", "trusted_context_required")
    return context, binding, conversation, channel, None


def _parse_subcommand(content: str) -> str:
    normalized = str(content or "").strip()
    if normalized[:1] in {"/", "#"}:
        normalized = normalized[1:].lstrip()
    if normalized == COMMAND:
        return "帮助"
    if not normalized.startswith(f"{COMMAND} "):
        return "帮助"
    return normalized[len(COMMAND) :].strip().split(maxsplit=1)[0]


def _help_result(config: LuckyPotConfig) -> PluginExecutionResult:
    reply_text = (
        "大奖池用法：\n"
        "大奖池 创建\n"
        "大奖池 参与\n"
        "大奖池 查看\n"
        "大奖池 开奖\n"
        "大奖池 取消\n"
        f"当前入场费 {config.entry_fee} 积分，{config.countdown_seconds} 秒后自动结算。"
    )
    return _text_result(reply_text, "help")


def _load_config(raw_config: Mapping[str, Any]) -> LuckyPotConfig:
    raw = dict(raw_config)
    max_players = _bounded_int(raw.get("max_players"), default=10, minimum=2, maximum=MAX_PLAYERS_LIMIT)
    min_players = _bounded_int(raw.get("min_players"), default=2, minimum=2, maximum=max_players)
    return LuckyPotConfig(
        entry_fee=_bounded_int(raw.get("entry_fee"), default=100, minimum=1, maximum=1_000_000),
        min_players=min_players,
        max_players=max_players,
        countdown_seconds=_bounded_int(raw.get("countdown_seconds"), default=120, minimum=10, maximum=3600),
        house_cut_percent=_bounded_int(raw.get("house_cut_percent"), default=5, minimum=0, maximum=30),
        lucky_bonus_chance=_bounded_int(raw.get("lucky_bonus_chance"), default=20, minimum=0, maximum=100),
        lucky_bonus_percent=_bounded_int(raw.get("lucky_bonus_percent"), default=20, minimum=0, maximum=50),
    )


def _state_key(context: PluginExecutionContext) -> str:
    return f"{STATE_KEY_PREFIX}:{int(context.conversation_id or 0)}"


def _stable_room_id(context: PluginExecutionContext) -> str:
    """同一条创建消息始终生成同一房间号，渠道重试不会产生第二次扣款定位。"""

    raw = f"{int(context.binding_id)}:{int(context.conversation_id or 0)}:{int(context.message_id or 0)}"
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:12]


def _entry_key(context: PluginExecutionContext, room_id: str, user_id: str) -> str:
    return f"lucky-pot:{int(context.binding_id)}:{int(context.conversation_id or 0)}:{room_id}:entry:{user_id}"


def _text_outbound_payload(conversation: Conversation, content: str) -> dict[str, Any]:
    return {
        "external_id": str(conversation.external_id or "").strip(),
        "receiver_id": str(conversation.external_id or "").strip(),
        "chat_type": "group",
        "message_type": "text",
        "content": content,
    }


def _status_text(status: str) -> str:
    return {
        "waiting": "等待参与",
        "drawing": "开奖中",
        "refunding": "退款中",
    }.get(status, "处理中")


def _text_result(text: str, provider_status: str, **output: Any) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text,
        output={
            "provider_status": provider_status,
            **output,
            "replies": standard_replies([text_reply(text)]),
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


def _single_line(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(resolved, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
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
