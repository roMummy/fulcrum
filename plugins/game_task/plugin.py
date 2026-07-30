from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.time import now_shanghai
from app.core.db import SessionLocal
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


JOIN_COMMANDS = {"/s"}
TASK_COMMANDS = {"/t"}
ANSWER_COMMANDS = {"/a"}
RANK_COMMANDS = {"/r"}
ACTIVE_COMMANDS = {"/l"}
HELP_COMMANDS = {"/h", "/help"}
DEFAULT_COMMANDS = tuple(sorted(JOIN_COMMANDS | TASK_COMMANDS | ANSWER_COMMANDS | RANK_COMMANDS | ACTIVE_COMMANDS | HELP_COMMANDS))

QUESTION_BANK = [
    {"question": "中国的首都是哪里？", "answer": "北京", "score": 3, "category": "常识", "hint": "这是中国的政治中心。"},
    {"question": "一年有多少个月？", "answer": "12", "score": 2, "category": "常识", "hint": "不是 10，也不是 13。"},
    {"question": "水的化学式是什么？", "answer": "h2o", "score": 4, "category": "科学", "hint": "由氢和氧组成。"},
    {"question": "太阳从哪边升起？", "answer": "东边", "score": 2, "category": "地理", "hint": "和西边相反。"},
    {"question": "Python 最常见的文件扩展名是什么？", "answer": ".py", "score": 4, "category": "技术", "hint": "两个字符，以点开头。"},
]


@dataclass
class ActiveTask:
    task_id: int
    room_id: str
    holder_id: str
    holder_name: str
    question: str
    answer: str
    score: int
    category: str
    hint: str
    created_at: datetime = field(default_factory=now_shanghai)
    status: str = "active"


@dataclass
class RoomState:
    players: dict[str, str] = field(default_factory=dict)
    active_tasks: dict[int, ActiveTask] = field(default_factory=dict)
    next_task_id: int = 1


ROOM_STATES: dict[str, RoomState] = {}


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="game_task runtime is ready",
            output={
                "enabled": config["enabled"],
                "commands": list(config["commands"]),
                "question_count": len(QUESTION_BANK),
                "default_score_multiplier": config["score_multiplier"],
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")
    if not config["enabled"]:
        return PluginExecutionResult(ok=False, status="disabled", message="game_task is disabled")

    content = str(request.payload.get("content") or "").strip()
    if not content:
        return PluginExecutionResult(ok=True, status="completed", message="empty content", output={"provider_status": "ignored"})

    command = content.split()[0].lower()
    if command in HELP_COMMANDS:
        return _help_result()
    if command in JOIN_COMMANDS:
        return _handle_join(request)
    if command in TASK_COMMANDS:
        return _handle_task(request, score_multiplier=config["score_multiplier"])
    if command in ANSWER_COMMANDS:
        return _handle_answer(request, score_multiplier=config["score_multiplier"])
    if command in RANK_COMMANDS:
        return _handle_rank(request)
    if command in ACTIVE_COMMANDS:
        return _handle_active_tasks(request)
    return PluginExecutionResult(ok=True, status="completed", message="not a game task command", output={"provider_status": "ignored"})


def _handle_join(request: PluginExecutionRequest) -> PluginExecutionResult:
    room_id = _resolve_room_id(request.payload)
    sender_id = _resolve_sender_id(request.payload)
    sender_name = _resolve_sender_name(request.payload, sender_id)
    if not room_id or not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="join requires group room_id and sender_id")

    state = ROOM_STATES.setdefault(room_id, RoomState())
    already_joined = sender_id in state.players
    state.players[sender_id] = sender_name
    reply_text = f"{sender_name} 已加入问答游戏。" if not already_joined else f"{sender_name} 已经在游戏里了。"
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=reply_text,
        output={"provider_status": "joined", "room_id": room_id, "player_count": len(state.players), "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]},
    )


def _handle_task(request: PluginExecutionRequest, *, score_multiplier: int) -> PluginExecutionResult:
    room_id = _resolve_room_id(request.payload)
    sender_id = _resolve_sender_id(request.payload)
    sender_name = _resolve_sender_name(request.payload, sender_id)
    if not room_id or not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="task requires group room_id and sender_id")

    state = ROOM_STATES.setdefault(room_id, RoomState())
    if sender_id not in state.players:
        state.players[sender_id] = sender_name

    bank_item = random.choice(QUESTION_BANK)
    task_id = state.next_task_id
    state.next_task_id += 1
    task = ActiveTask(
        task_id=task_id,
        room_id=room_id,
        holder_id=sender_id,
        holder_name=sender_name,
        question=str(bank_item["question"]),
        answer=str(bank_item["answer"]).strip().lower(),
        score=int(bank_item["score"]) * max(score_multiplier, 1),
        category=str(bank_item["category"]),
        hint=str(bank_item["hint"]),
    )
    state.active_tasks[task_id] = task
    reply_text = (
        f"任务 #{task_id}\n"
        f"类别: {task.category}\n"
        f"题目: {task.question}\n"
        f"分值: {task.score}\n"
        f"作答方式: /a {task_id} 你的答案"
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="task created",
        output={
            "provider_status": "task_created",
            "task_id": task_id,
            "task": _serialize_task(task),
            "reply_text": reply_text,
            "replies": [{"type": "text", "content": reply_text}],
        },
    )


def _handle_answer(request: PluginExecutionRequest, *, score_multiplier: int) -> PluginExecutionResult:
    room_id = _resolve_room_id(request.payload)
    sender_id = _resolve_sender_id(request.payload)
    sender_name = _resolve_sender_name(request.payload, sender_id)
    if not room_id or not sender_id:
        return PluginExecutionResult(ok=False, status="error", message="answer requires group room_id and sender_id")

    state = ROOM_STATES.setdefault(room_id, RoomState())
    if sender_id not in state.players:
        state.players[sender_id] = sender_name

    parts = str(request.payload.get("content") or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        return PluginExecutionResult(ok=False, status="error", message="usage: /a task_id answer")
    try:
        task_id = int(parts[1])
    except ValueError:
        return PluginExecutionResult(ok=False, status="error", message="task_id must be an integer")
    answer = parts[2].strip().lower()
    task = state.active_tasks.get(task_id)
    if task is None or task.status != "active":
        return PluginExecutionResult(ok=False, status="error", message=f"task #{task_id} not found or already completed")

    if answer == task.answer:
        task.status = "completed"
        awarded = max(task.score, 1) * max(score_multiplier, 1)
        total_points = _award_points(sender_id, room_id, sender_name, awarded, task)
        state.active_tasks.pop(task_id, None)
        reply_text = (
            f"{sender_name} 回答正确，获得 {awarded} 积分。\n"
            f"题目: {task.question}\n"
            f"提示: {task.hint}\n"
            f"当前积分: {total_points}"
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="answer accepted",
            output={
                "provider_status": "answered_correct",
                "task_id": task_id,
                "awarded_points": awarded,
                "total_points": total_points,
                "task": _serialize_task(task),
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )

    penalty = 1
    total_points = _award_points(sender_id, room_id, sender_name, -penalty, task)
    reply_text = (
        f"{sender_name} 回答错误，扣除 {penalty} 积分。\n"
        f"任务 #{task_id} 仍然有效，可以继续抢答。\n"
        f"提示: {task.hint}\n"
        f"当前积分: {total_points}"
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="answer incorrect",
        output={
            "provider_status": "answered_incorrect",
            "task_id": task_id,
            "penalty_points": penalty,
            "total_points": total_points,
            "task": _serialize_task(task),
            "reply_text": reply_text,
            "replies": [{"type": "text", "content": reply_text}],
        },
    )


def _handle_rank(request: PluginExecutionRequest) -> PluginExecutionResult:
    room_id = _resolve_room_id(request.payload)
    if not room_id:
        return PluginExecutionResult(ok=False, status="error", message="rank requires group room_id")
    state = ROOM_STATES.setdefault(room_id, RoomState())
    if not state.players:
        return PluginExecutionResult(ok=True, status="completed", message="当前还没有玩家加入。", output={"provider_status": "empty", "reply_text": "当前还没有玩家加入。", "replies": [{"type": "text", "content": "当前还没有玩家加入。"}]})

    ranking = _load_points_ranking(room_id, state.players)
    lines = ["问答积分榜"]
    for index, item in enumerate(ranking, start=1):
        lines.append(f"{index}. {item['user_name']} {item['points']} 分")
    reply_text = "\n".join(lines)
    return PluginExecutionResult(ok=True, status="completed", message="ranking ready", output={"provider_status": "ranking", "ranking": ranking, "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _handle_active_tasks(request: PluginExecutionRequest) -> PluginExecutionResult:
    room_id = _resolve_room_id(request.payload)
    if not room_id:
        return PluginExecutionResult(ok=False, status="error", message="active list requires group room_id")
    state = ROOM_STATES.setdefault(room_id, RoomState())
    active_tasks = [task for task in state.active_tasks.values() if task.status == "active"]
    if not active_tasks:
        text = "当前没有活跃任务，发送 /t 来领取一题。"
        return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "empty", "reply_text": text, "replies": [{"type": "text", "content": text}]})

    lines = ["当前活跃任务"]
    for task in sorted(active_tasks, key=lambda item: item.task_id):
        lines.append(f"#{task.task_id} {task.category} {task.question} ({task.score}分)")
    reply_text = "\n".join(lines)
    return PluginExecutionResult(ok=True, status="completed", message="active tasks ready", output={"provider_status": "active_tasks", "tasks": [_serialize_task(task) for task in active_tasks], "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _help_result() -> PluginExecutionResult:
    reply_text = (
        "问答游戏命令:\n"
        "/s 加入游戏\n"
        "/t 获取一题\n"
        "/a 任务ID 答案\n"
        "/r 查看积分榜\n"
        "/l 查看活跃任务\n"
        "/h 查看帮助"
    )
    return PluginExecutionResult(ok=True, status="completed", message="help ready", output={"provider_status": "help", "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _award_points(user_id: str, room_id: str, user_name: str, delta: int, task: ActiveTask) -> int:
    with SessionLocal() as db:
        service = PointsService(db)
        account = service.ensure_account(user_id=user_id, group_id=room_id, user_name=user_name)
        service.record_transaction(
            account=account,
            delta=delta,
            source="game_task",
            description=f"问答任务 #{task.task_id}: {task.question[:40]}",
        )
        db.commit()
        db.refresh(account)
        return int(account.total_points or 0)


def _load_points_ranking(room_id: str, players: dict[str, str]) -> list[dict[str, Any]]:
    ranking: list[dict[str, Any]] = []
    with SessionLocal() as db:
        service = PointsService(db)
        for user_id, user_name in players.items():
            account = service.ensure_account(user_id=user_id, group_id=room_id, user_name=user_name)
            ranking.append({"user_id": user_id, "user_name": user_name, "points": int(account.total_points or 0)})
        db.commit()
    ranking.sort(key=lambda item: (-item["points"], item["user_name"]))
    return ranking


def _serialize_task(task: ActiveTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "room_id": task.room_id,
        "holder_id": task.holder_id,
        "holder_name": task.holder_name,
        "question": task.question,
        "score": task.score,
        "category": task.category,
        "hint": task.hint,
        "status": task.status,
        "created_at": task.created_at.isoformat(),
    }


def _load_config(request: PluginExecutionRequest) -> dict[str, Any]:
    raw = dict(request.config)
    commands_raw = raw.get("commands")
    commands = list(commands_raw) if isinstance(commands_raw, list) and commands_raw else list(DEFAULT_COMMANDS)
    return {
        "enabled": _as_bool(raw.get("enabled"), default=True),
        "commands": commands,
        "score_multiplier": _as_int(raw.get("score_multiplier"), default=1, minimum=1),
    }


def _resolve_room_id(payload: dict[str, Any]) -> str:
    normalized_event = payload.get("normalized_event")
    if isinstance(normalized_event, dict):
        conversation = normalized_event.get("conversation")
        if isinstance(conversation, dict):
            room_id = str(conversation.get("external_id") or "").strip()
            if room_id.endswith("@chatroom"):
                return room_id
    room_id = str(payload.get("conversation_external_id") or "").strip()
    return room_id if room_id.endswith("@chatroom") else ""


def _resolve_sender_id(payload: dict[str, Any]) -> str:
    return str(payload.get("sender_id") or payload.get("user") or "").strip()


def _resolve_sender_name(payload: dict[str, Any], sender_id: str) -> str:
    return str(payload.get("sender_name") or sender_id or "玩家").strip() or "玩家"


def _as_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: object, *, default: int, minimum: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, resolved)
