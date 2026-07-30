from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field

from app.core.db import SessionLocal
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


@dataclass
class LuckyPotRoom:
    room_id: str
    group_id: str
    creator_id: str
    creator_name: str
    entry_fee: int
    max_players: int
    house_cut_percent: int
    lucky_bonus_chance: int
    lucky_bonus_percent: int
    players: list[dict[str, str]] = field(default_factory=list)
    status: str = "waiting"
    result: dict[str, object] | None = None

    def has_player(self, user_id: str) -> bool:
        return any(item["user_id"] == user_id for item in self.players)

    def total_pot(self) -> int:
        return self.entry_fee * len(self.players)


ACTIVE_ROOMS: dict[str, LuckyPotRoom] = {}


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="lucky_pot runtime is ready", output={"active_rooms": len(ACTIVE_ROOMS)})
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    command = _resolve_subcommand(request.payload)
    if command in {"create", "创建"}:
        return _create_room(request)
    if command in {"join", "参与"}:
        return _join_room(request)
    if command in {"view", "查看"}:
        return _view_room(request)
    if command in {"cancel", "取消"}:
        return _cancel_room(request)
    if command in {"draw", "开奖"}:
        return _draw_room(request)
    return _help_message(request)


def _create_room(request: PluginExecutionRequest) -> PluginExecutionResult:
    group_id = _resolve_group_id(request.payload)
    sender_id = _resolve_sender_id(request.payload)
    sender_name = _resolve_sender_name(request.payload, sender_id)
    if not group_id or not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="大奖池仅支持群聊。")
    if group_id in ACTIVE_ROOMS and ACTIVE_ROOMS[group_id].status == "waiting":
        room = ACTIVE_ROOMS[group_id]
        return PluginExecutionResult(
            ok=False,
            status="error",
            message=f"当前已有进行中的大奖池，人数 {len(room.players)}/{room.max_players}，奖池 {room.total_pot()} 积分。",
        )

    entry_fee = max(_coerce_int(request.config.get("entry_fee"), 100), 1)
    with SessionLocal() as db:
        points_service = PointsService(db)
        account = points_service.get_user_points(sender_id, group_id)
        current_points = int((account or {}).get("total_points") or 0)
        if current_points < entry_fee:
            return PluginExecutionResult(ok=False, status="error", message=f"积分不足，创建大奖池需要 {entry_fee} 积分，当前仅有 {current_points}。")
        deduct_result = points_service.deduct_points(sender_id, group_id, entry_fee, user_name=sender_name, source="lucky_pot_join", description="创建大奖池")
        if not deduct_result.ok:
            return PluginExecutionResult(ok=False, status="error", message=deduct_result.message)

    room = LuckyPotRoom(
        room_id=str(uuid.uuid4())[:8],
        group_id=group_id,
        creator_id=sender_id,
        creator_name=sender_name,
        entry_fee=entry_fee,
        max_players=max(_coerce_int(request.config.get("max_players"), 10), 2),
        house_cut_percent=max(_coerce_int(request.config.get("house_cut_percent"), 5), 0),
        lucky_bonus_chance=max(_coerce_int(request.config.get("lucky_bonus_chance"), 20), 0),
        lucky_bonus_percent=max(_coerce_int(request.config.get("lucky_bonus_percent"), 20), 0),
    )
    room.players.append({"user_id": sender_id, "user_name": sender_name})
    ACTIVE_ROOMS[group_id] = room
    reply_text = (
        f"大奖池已创建。"
        f"\n房间 ID: {room.room_id}"
        f"\n入场费 {room.entry_fee} 积分，当前 1/{room.max_players} 人。"
        f"\n发送 lucky_pot join 加入，或 lucky_pot draw 立即开奖。"
    )
    return PluginExecutionResult(ok=True, status="completed", message="大奖池创建成功。", output={"provider_status": "created", "room_id": room.room_id, "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _join_room(request: PluginExecutionRequest) -> PluginExecutionResult:
    group_id = _resolve_group_id(request.payload)
    sender_id = _resolve_sender_id(request.payload)
    sender_name = _resolve_sender_name(request.payload, sender_id)
    if not group_id or not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="大奖池仅支持群聊。")
    room = ACTIVE_ROOMS.get(group_id)
    if room is None or room.status != "waiting":
        return PluginExecutionResult(ok=False, status="error", message="当前没有可加入的大奖池。")
    if room.has_player(sender_id):
        return PluginExecutionResult(ok=False, status="error", message="你已经在大奖池里了。")
    if len(room.players) >= room.max_players:
        return PluginExecutionResult(ok=False, status="error", message="大奖池人数已满。")

    with SessionLocal() as db:
        points_service = PointsService(db)
        account = points_service.get_user_points(sender_id, group_id)
        current_points = int((account or {}).get("total_points") or 0)
        if current_points < room.entry_fee:
            return PluginExecutionResult(ok=False, status="error", message=f"积分不足，加入大奖池需要 {room.entry_fee} 积分，当前仅有 {current_points}。")
        deduct_result = points_service.deduct_points(sender_id, group_id, room.entry_fee, user_name=sender_name, source="lucky_pot_join", description=f"加入大奖池 {room.room_id}")
        if not deduct_result.ok:
            return PluginExecutionResult(ok=False, status="error", message=deduct_result.message)

    room.players.append({"user_id": sender_id, "user_name": sender_name})
    reply_text = f"{sender_name} 加入成功，当前 {len(room.players)}/{room.max_players} 人，奖池 {room.total_pot()} 积分。"
    return PluginExecutionResult(ok=True, status="completed", message="加入大奖池成功。", output={"provider_status": "joined", "room_id": room.room_id, "player_count": len(room.players), "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _view_room(request: PluginExecutionRequest) -> PluginExecutionResult:
    group_id = _resolve_group_id(request.payload)
    room = ACTIVE_ROOMS.get(group_id or "")
    if room is None:
        return PluginExecutionResult(ok=False, status="error", message="当前没有活动中的大奖池。")
    player_names = "、".join(item["user_name"] for item in room.players)
    reply_text = (
        f"大奖池状态：{room.status}"
        f"\n房间 ID: {room.room_id}"
        f"\n当前人数 {len(room.players)}/{room.max_players}"
        f"\n当前奖池 {room.total_pot()} 积分"
        f"\n参与玩家：{player_names}"
    )
    return PluginExecutionResult(ok=True, status="completed", message="大奖池查询成功。", output={"provider_status": "view", "room_id": room.room_id, "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _cancel_room(request: PluginExecutionRequest) -> PluginExecutionResult:
    group_id = _resolve_group_id(request.payload)
    sender_id = _resolve_sender_id(request.payload)
    room = ACTIVE_ROOMS.get(group_id or "")
    if room is None:
        return PluginExecutionResult(ok=False, status="error", message="当前没有活动中的大奖池。")
    if sender_id != room.creator_id:
        return PluginExecutionResult(ok=False, status="error", message="只有创建者可以取消大奖池。")

    with SessionLocal() as db:
        points_service = PointsService(db)
        for item in room.players:
            points_service.add_points(
                item["user_id"],
                room.group_id,
                room.entry_fee,
                user_name=item["user_name"],
                source="lucky_pot_refund",
                description=f"大奖池 {room.room_id} 取消退款",
            )

    del ACTIVE_ROOMS[room.group_id]
    reply_text = "大奖池已取消，所有参与玩家的入场积分都已退还。"
    return PluginExecutionResult(ok=True, status="completed", message="大奖池已取消。", output={"provider_status": "cancelled", "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _draw_room(request: PluginExecutionRequest) -> PluginExecutionResult:
    group_id = _resolve_group_id(request.payload)
    room = ACTIVE_ROOMS.get(group_id or "")
    if room is None:
        return PluginExecutionResult(ok=False, status="error", message="当前没有活动中的大奖池。")
    if len(room.players) < 1:
        return PluginExecutionResult(ok=False, status="error", message="大奖池里还没有参与者。")

    total_pot = room.total_pot()
    house_cut = int(total_pot * room.house_cut_percent / 100)
    distributable = max(total_pot - house_cut, len(room.players))
    rewards = _distribute_rewards([item["user_id"] for item in room.players], distributable)
    lucky_winner = None
    lucky_bonus = 0
    if len(room.players) > 1 and random.randint(1, 100) <= room.lucky_bonus_chance:
        lucky_winner = random.choice(room.players)["user_id"]
        lucky_bonus = max(1, int(distributable * room.lucky_bonus_percent / 100))
        rewards[lucky_winner] = rewards.get(lucky_winner, 0) + lucky_bonus

    with SessionLocal() as db:
        points_service = PointsService(db)
        for item in room.players:
            reward = rewards.get(item["user_id"], 0)
            if reward > 0:
                points_service.add_points(
                    item["user_id"],
                    room.group_id,
                    reward,
                    user_name=item["user_name"],
                    source="lucky_pot_reward",
                    description=f"大奖池 {room.room_id} 开奖",
                )

    ranking = sorted(room.players, key=lambda player: rewards.get(player["user_id"], 0), reverse=True)
    lines = [f"大奖池开奖完成。", f"总奖池 {total_pot}，系统抽水 {house_cut}。"]
    for index, item in enumerate(ranking, start=1):
        reward = rewards.get(item["user_id"], 0)
        profit = reward - room.entry_fee
        lines.append(f"{index}. {item['user_name']} 获得 {reward} 积分，净收益 {profit:+d}")
    if lucky_winner:
        lucky_name = next((item["user_name"] for item in room.players if item["user_id"] == lucky_winner), lucky_winner)
        lines.append(f"欧皇加成：{lucky_name} 额外获得 {lucky_bonus} 积分。")

    room.status = "finished"
    room.result = {"rewards": rewards, "house_cut": house_cut, "total_pot": total_pot, "lucky_winner": lucky_winner, "lucky_bonus": lucky_bonus}
    del ACTIVE_ROOMS[room.group_id]
    reply_text = "\n".join(lines)
    return PluginExecutionResult(ok=True, status="completed", message="大奖池开奖完成。", output={"provider_status": "drawn", "result": room.result, "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _help_message(request: PluginExecutionRequest) -> PluginExecutionResult:
    entry_fee = max(_coerce_int(request.config.get("entry_fee"), 100), 1)
    reply_text = (
        "幸运池用法：\n"
        "lucky_pot create 创建房间\n"
        "lucky_pot join 加入房间\n"
        "lucky_pot view 查看状态\n"
        "lucky_pot draw 立即开奖\n"
        "lucky_pot cancel 取消并退款\n"
        f"当前默认入场费 {entry_fee} 积分。"
    )
    return PluginExecutionResult(ok=True, status="completed", message="已返回帮助信息。", output={"provider_status": "help", "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _resolve_subcommand(payload: dict[str, object]) -> str:
    args = str(payload.get("command_args") or payload.get("stripped_content") or "").strip().split()
    if args:
        return args[0].strip().lower()
    return "help"


def _resolve_sender_id(payload: dict[str, object]) -> str:
    return str(payload.get("sender_id") or payload.get("user") or "").strip()


def _resolve_sender_name(payload: dict[str, object], fallback: str) -> str:
    return str(payload.get("sender_name") or payload.get("user_name") or fallback).strip() or fallback


def _resolve_group_id(payload: dict[str, object]) -> str:
    if str(payload.get("chat_type") or "").strip() != "group":
        return ""
    return str(payload.get("conversation_external_id") or payload.get("room_id") or "").strip()


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _distribute_rewards(players: list[str], total_points: int) -> dict[str, int]:
    if len(players) == 1:
        return {players[0]: total_points}
    remaining = total_points
    rewards: list[int] = []
    for index in range(len(players) - 1):
        max_share = max(1, remaining - (len(players) - index - 1))
        share = random.randint(1, max_share)
        rewards.append(share)
        remaining -= share
    rewards.append(remaining)
    random.shuffle(rewards)
    return dict(zip(players, rewards))
