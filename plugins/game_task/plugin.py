from __future__ import annotations

import random
import re
from dataclasses import dataclass
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
from app.services.plugin_runtime.state_store import PluginStateStore


STATE_NAMESPACE = "game_task"
STATE_KEY_PREFIX = "conversation"
MAX_PLAYERS = 200
MAX_STORED_ACTIVE_TASKS = 20
MAX_TASK_ID = 2_000_000_000
COMMANDS = {"s", "t", "a", "r", "l", "h"}

# 当前版本使用平台内置的稳定题库，不在插件里共享平台 LLM 密钥，也不让租户为
# 基础问答承担不可预期的模型费用。以后需要扩展题库时，应接入独立的租户题库能力。
QUESTION_BANK = (
    {"question": "中国的首都是哪里？", "answer": "北京", "score": 3, "category": "常识", "hint": "这是中国的政治中心。"},
    {"question": "一年有多少个月？", "answer": "12", "score": 2, "category": "常识", "hint": "不是 10，也不是 13。"},
    {"question": "水的化学式是什么？", "answer": "h2o", "score": 4, "category": "科学", "hint": "由氢和氧组成。"},
    {"question": "太阳从哪边升起？", "answer": "东边", "score": 2, "category": "地理", "hint": "和西边相反。"},
    {"question": "Python 最常见的文件扩展名是什么？", "answer": ".py", "score": 4, "category": "技术", "hint": "两个字符，以点开头。"},
)


class GameTaskStateError(ValueError):
    """游戏状态损坏或超出当前产品容量边界。"""


@dataclass(frozen=True)
class GameTaskConfig:
    """当前绑定的问答规则和随机发题计划。"""

    score_multiplier: int
    ranking_limit: int
    max_active_tasks: int
    random_task_enabled: bool
    random_task_time: str
    random_task_conversation_ids: tuple[int, ...]


@dataclass
class RoomState:
    """一个可信群会话内需要持久化的最小游戏状态。"""

    players: dict[str, str]
    active_tasks: dict[int, dict[str, Any]]
    next_task_id: int


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供重载检查，业务执行必须拥有数据库可信上下文。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="游戏任务插件已就绪。",
            output={
                "question_count": len(QUESTION_BANK),
                "score_multiplier": config.score_multiplier,
                "random_task_enabled": config.random_task_enabled,
                "random_task_target_count": len(config.random_task_conversation_ids),
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("游戏任务只能通过真实群聊消息执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """使用已落库消息处理问答命令，忽略载荷伪造的群、用户和昵称。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)

    context, conversation, message, error = _require_group_message_context(db, request.context)
    if error is not None or context is None or conversation is None or message is None:
        return error or _error_result("游戏任务缺少可信群聊消息。", "trusted_context_required")

    command, arguments = _parse_command(message.content)
    if command not in COMMANDS:
        return _error_result("当前消息不是游戏任务命令。", "command_not_matched")

    sender_id = str(message.sender_id or "").strip()
    sender_name = _single_line(message.sender_name or sender_id, limit=64) or sender_id
    store = PluginStateStore(db, context)
    state_key = _state_key(context)
    try:
        state = _load_room_state(store, state_key)
    except GameTaskStateError:
        return _error_result("当前群的游戏状态异常，请联系管理员重置插件状态。", "state_invalid")

    if command == "s":
        return _handle_join(store, state_key, state, sender_id, sender_name)
    if command == "t":
        return _handle_create_task(
            store,
            state_key,
            state,
            sender_id=sender_id,
            sender_name=sender_name,
            config=config,
        )
    if command == "a":
        return _handle_answer(
            db,
            store,
            state_key,
            state,
            context=context,
            conversation=conversation,
            message=message,
            sender_id=sender_id,
            sender_name=sender_name,
            arguments=arguments,
        )
    if command == "r":
        return _handle_ranking(db, state, conversation, config)
    if command == "l":
        return _handle_active_tasks(state, detailed=True)
    return _handle_active_tasks(state, detailed=False)


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在平台配置的单个群会话中随机选择已报名玩家并创建出站发题任务。"""

    if request.action != "random_task_assignment":
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的游戏任务定时动作: {request.action}",
        )
    config = _load_config(request.config)
    context, binding, conversation, channel, error = _require_scheduled_group_context(db, request.context)
    if error is not None or context is None or binding is None or conversation is None or channel is None:
        return error or _error_result("随机发题缺少可信群聊上下文。", "trusted_context_required")
    if not config.random_task_enabled:
        return _scheduled_skip_result("当前绑定未开启随机发题。", "disabled")
    if int(conversation.id) not in config.random_task_conversation_ids:
        return _error_result("当前群不在随机发题范围内。", "conversation_not_configured")

    store = PluginStateStore(db, context)
    state_key = _state_key(context)
    try:
        state = _load_room_state(store, state_key)
    except GameTaskStateError:
        return _error_result("当前群的游戏状态异常，无法随机发题。", "state_invalid")
    if not state.players:
        return _scheduled_skip_result("当前群还没有报名玩家，已跳过随机发题。", "no_players")
    if len(state.active_tasks) >= config.max_active_tasks:
        return _scheduled_skip_result("当前群未完成任务已达到上限，已跳过随机发题。", "task_limit_reached")

    holder_id, holder_name = random.choice(sorted(state.players.items()))
    task = _append_task(
        state,
        holder_id=holder_id,
        holder_name=holder_name,
        score_multiplier=config.score_multiplier,
    )
    _save_room_state(store, state_key, state)
    reply_text = _task_prompt(task, scheduled=True)
    job_ids = enqueue_outbound_jobs(
        db,
        conversation=conversation,
        channel=channel,
        payloads=[
            {
                "external_id": str(conversation.external_id or "").strip(),
                "receiver_id": str(conversation.external_id or "").strip(),
                "chat_type": "group",
                "message_type": "text",
                "content": reply_text,
            }
        ],
        plugin_code="game_task",
        related_type="game_task_binding",
        related_id=int(binding.id),
        priority=60,
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="已创建随机问答任务。",
        output={
            "provider_status": "task_assigned",
            "conversation_id": int(conversation.id),
            "task_id": int(task["task_id"]),
            "outbound_job_ids": [int(item) for item in job_ids],
        },
    )


def _handle_join(
    store: PluginStateStore,
    state_key: str,
    state: RoomState,
    sender_id: str,
    sender_name: str,
) -> PluginExecutionResult:
    """报名只记录可信消息发送人，达到容量上限后拒绝继续扩张状态。"""

    already_joined = sender_id in state.players
    if not already_joined and len(state.players) >= MAX_PLAYERS:
        return _error_result("当前群的游戏玩家已达到上限。", "player_limit_reached")
    state.players[sender_id] = sender_name
    _save_room_state(store, state_key, state)
    if already_joined:
        return _text_result(f"{sender_name} 已经报名，可以发送 /t 领取题目。", "already_joined")
    return _text_result(f"{sender_name} 已加入问答游戏，发送 /t 领取题目。", "joined")


def _handle_create_task(
    store: PluginStateStore,
    state_key: str,
    state: RoomState,
    *,
    sender_id: str,
    sender_name: str,
    config: GameTaskConfig,
) -> PluginExecutionResult:
    """已报名玩家可以领题，单群活跃题目数量受绑定配置限制。"""

    if sender_id not in state.players:
        return _error_result("请先发送 /s 报名，再领取题目。", "player_not_joined")
    if len(state.active_tasks) >= config.max_active_tasks:
        return _error_result("当前未完成题目较多，请先作答后再领取。", "task_limit_reached")

    # 玩家昵称以最近一条可信消息为准，避免长期保存过期显示名。
    state.players[sender_id] = sender_name
    task = _append_task(
        state,
        holder_id=sender_id,
        holder_name=sender_name,
        score_multiplier=config.score_multiplier,
    )
    _save_room_state(store, state_key, state)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="已创建问答任务。",
        output={
            "provider_status": "task_created",
            "task_id": int(task["task_id"]),
            "active_task_count": len(state.active_tasks),
            # 执行日志只保存任务编号和标准回复，不复制答案及完整状态对象。
            "replies": standard_replies([text_reply(_task_prompt(task, scheduled=False))]),
        },
    )


def _handle_answer(
    db: Session,
    store: PluginStateStore,
    state_key: str,
    state: RoomState,
    *,
    context: PluginExecutionContext,
    conversation: Conversation,
    message: Message,
    sender_id: str,
    sender_name: str,
    arguments: str,
) -> PluginExecutionResult:
    """按任务和消息幂等结算积分，奖励键不允许同一题产生多个获奖人。"""

    if sender_id not in state.players:
        return _error_result("请先发送 /s 报名，再参与答题。", "player_not_joined")
    parts = arguments.split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].strip():
        return _error_result("命令格式：/a 任务ID 答案", "invalid_parameters")
    task_id = int(parts[0])
    task = state.active_tasks.get(task_id)
    if task is None:
        return _error_result(f"任务 #{task_id} 不存在或已经完成。", "task_not_found")

    answer = _normalize_answer(parts[1])
    correct_answer = _normalize_answer(task.get("answer"))
    points_service = PointsService(db)
    if answer == correct_answer:
        # 先把任务移出活跃集合，再由积分服务提交同一数据库事务。重复投递时即使
        # 再次进入积分服务，任务级幂等键也只允许一条奖励流水。
        state.active_tasks.pop(task_id, None)
        _save_room_state(store, state_key, state)
        reward = int(task["score"])
        points_result = points_service.earn(
            context,
            reward,
            source="game_task",
            description=f"问答任务 #{task_id} 奖励",
            metadata={"conversation_id": int(conversation.id), "task_id": task_id},
            idempotency_key=(
                f"game-task:{int(context.binding_id)}:{int(conversation.id)}:{task_id}:reward"
            ),
        )
        if not points_result.ok:
            db.rollback()
            return _error_result(points_result.message, "points_failed")
        balance = int((points_result.account or {}).get("total_points") or 0)
        text = f"{sender_name} 回答正确，获得 {reward} 积分。当前积分 {balance}。"
        return _text_result(
            text,
            "answered_correct",
            task_id=task_id,
            awarded_points=reward,
            total_points=balance,
        )

    # 错答流水使用可信消息主键，渠道重试同一消息时不会重复扣分。积分不足时不
    # 产生负余额，题目仍保持有效，其他已报名玩家可以继续作答。
    penalty_result = points_service.spend(
        context,
        1,
        source="game_task",
        description=f"问答任务 #{task_id} 错答扣分",
        metadata={"conversation_id": int(conversation.id), "task_id": task_id},
        idempotency_key=(
            f"game-task:{int(context.binding_id)}:{int(conversation.id)}:{task_id}:wrong:{int(message.id)}"
        ),
    )
    balance = int((penalty_result.account or {}).get("total_points") or 0)
    if not penalty_result.ok:
        text = f"{sender_name} 回答不正确。当前积分不足，本次未扣分。提示：{task['hint']}"
        return _text_result(text, "answered_incorrect_no_penalty", task_id=task_id, total_points=balance)
    repeated = bool((penalty_result.extra or {}).get("idempotent"))
    text = (
        f"{sender_name} 的这次错答已经处理，任务仍然有效。当前积分 {balance}。"
        if repeated
        else f"{sender_name} 回答不正确，扣除 1 积分。提示：{task['hint']} 当前积分 {balance}。"
    )
    return _text_result(
        text,
        "answered_incorrect_repeated" if repeated else "answered_incorrect",
        task_id=task_id,
        penalty_points=0 if repeated else 1,
        total_points=balance,
    )


def _handle_ranking(
    db: Session,
    state: RoomState,
    conversation: Conversation,
    config: GameTaskConfig,
) -> PluginExecutionResult:
    """排行榜只展示当前群已报名玩家，不把其他积分账户误算成游戏成员。"""

    if not state.players:
        return _text_result("当前群还没有玩家报名。", "empty")
    service = PointsService(db)
    group_id = str(conversation.external_id or "").strip()
    ranking: list[dict[str, Any]] = []
    for user_id, user_name in state.players.items():
        account = service.get_user_points(user_id, group_id) or {}
        ranking.append(
            {
                "user_id": user_id,
                "user_name": user_name,
                "points": int(account.get("total_points") or 0),
            }
        )
    ranking.sort(key=lambda item: (-int(item["points"]), str(item["user_name"]), str(item["user_id"])))
    ranking = ranking[: config.ranking_limit]
    lines = ["问答积分榜"]
    lines.extend(
        f"{index}. {item['user_name']} {item['points']} 分"
        for index, item in enumerate(ranking, start=1)
    )
    return _text_result("\n".join(lines), "ranking", ranking=ranking)


def _handle_active_tasks(state: RoomState, *, detailed: bool) -> PluginExecutionResult:
    """`/l` 展示题目详情，`/h` 提供紧凑的未完成任务清单。"""

    tasks = [state.active_tasks[key] for key in sorted(state.active_tasks)]
    if not tasks:
        return _text_result("当前没有未完成任务，发送 /t 领取一题。", "empty")
    lines = ["当前活跃任务" if detailed else "未完成任务"]
    for task in tasks:
        if detailed:
            lines.append(
                f"#{task['task_id']} [{task['category']}] {task['question']} "
                f"({task['score']} 分，领取人：{task['holder_name']})"
            )
        else:
            lines.append(f"#{task['task_id']} {task['category']}，使用 /a {task['task_id']} 答案 作答")
    return _text_result(
        "\n".join(lines),
        "active_tasks" if detailed else "unfinished_tasks",
        active_task_count=len(tasks),
    )


def _append_task(
    state: RoomState,
    *,
    holder_id: str,
    holder_name: str,
    score_multiplier: int,
) -> dict[str, Any]:
    """从固定题库创建最小任务状态，分值倍数只在创建时计算一次。"""

    if state.next_task_id <= 0 or state.next_task_id >= MAX_TASK_ID:
        raise GameTaskStateError("任务编号超过允许范围")
    item = random.choice(QUESTION_BANK)
    task_id = state.next_task_id
    state.next_task_id += 1
    task = {
        "task_id": task_id,
        "holder_id": holder_id,
        "holder_name": holder_name,
        "question": str(item["question"]),
        "answer": str(item["answer"]),
        "score": int(item["score"]) * score_multiplier,
        "category": str(item["category"]),
        "hint": str(item["hint"]),
        "created_at": now_shanghai().isoformat(),
    }
    state.active_tasks[task_id] = task
    return task


def _task_prompt(task: Mapping[str, Any], *, scheduled: bool) -> str:
    prefix = f"随机任务已分配给 {task['holder_name']}。" if scheduled else f"{task['holder_name']}，你的题目如下。"
    return (
        f"{prefix}\n"
        f"任务 #{task['task_id']}\n"
        f"类别：{task['category']}\n"
        f"题目：{task['question']}\n"
        f"分值：{task['score']}\n"
        f"作答：/a {task['task_id']} 你的答案"
    )


def _load_room_state(store: PluginStateStore, state_key: str) -> RoomState:
    """严格解析持久化状态，损坏数据不静默重置，避免积分任务被意外覆盖。"""

    payload = store.get(STATE_NAMESPACE, state_key)
    if payload is None:
        return RoomState(players={}, active_tasks={}, next_task_id=1)
    players_raw = payload.get("players")
    tasks_raw = payload.get("active_tasks")
    if not isinstance(players_raw, Mapping) or not isinstance(tasks_raw, Mapping):
        raise GameTaskStateError("游戏状态结构不正确")
    if len(players_raw) > MAX_PLAYERS or len(tasks_raw) > MAX_STORED_ACTIVE_TASKS:
        raise GameTaskStateError("游戏状态超过容量限制")

    players: dict[str, str] = {}
    for raw_user_id, raw_user_name in players_raw.items():
        user_id = _single_line(raw_user_id, limit=128)
        user_name = _single_line(raw_user_name, limit=64)
        if not user_id or not user_name:
            raise GameTaskStateError("玩家状态不完整")
        players[user_id] = user_name

    tasks: dict[int, dict[str, Any]] = {}
    for raw_task_id, raw_task in tasks_raw.items():
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError) as exc:
            raise GameTaskStateError("任务编号不合法") from exc
        if not isinstance(raw_task, Mapping) or task_id <= 0 or task_id > MAX_TASK_ID:
            raise GameTaskStateError("任务状态不合法")
        task = _normalize_stored_task(raw_task, task_id=task_id)
        tasks[task_id] = task

    next_task_id = _bounded_int(payload.get("next_task_id"), default=1, minimum=1, maximum=MAX_TASK_ID)
    if tasks and next_task_id <= max(tasks):
        raise GameTaskStateError("下一个任务编号与活跃任务冲突")
    return RoomState(players=players, active_tasks=tasks, next_task_id=next_task_id)


def _normalize_stored_task(raw: Mapping[str, Any], *, task_id: int) -> dict[str, Any]:
    """校验状态表中的任务字段，答案只允许在绑定状态中存在。"""

    holder_id = _single_line(raw.get("holder_id"), limit=128)
    holder_name = _single_line(raw.get("holder_name"), limit=64)
    question = _single_line(raw.get("question"), limit=500)
    answer = _single_line(raw.get("answer"), limit=200)
    category = _single_line(raw.get("category"), limit=40)
    hint = _single_line(raw.get("hint"), limit=300)
    score = _bounded_int(raw.get("score"), default=0, minimum=1, maximum=1000)
    created_at = _single_line(raw.get("created_at"), limit=64)
    if not all((holder_id, holder_name, question, answer, category, hint, created_at)):
        raise GameTaskStateError("任务字段不完整")
    return {
        "task_id": task_id,
        "holder_id": holder_id,
        "holder_name": holder_name,
        "question": question,
        "answer": answer,
        "score": score,
        "category": category,
        "hint": hint,
        "created_at": created_at,
    }


def _save_room_state(store: PluginStateStore, state_key: str, state: RoomState) -> None:
    """把内存状态转换成稳定 JSON，状态表始终按可信会话主键分组。"""

    if len(state.players) > MAX_PLAYERS or len(state.active_tasks) > MAX_STORED_ACTIVE_TASKS:
        raise GameTaskStateError("游戏状态超过容量限制")
    store.put(
        STATE_NAMESPACE,
        state_key,
        {
            "players": dict(sorted(state.players.items())),
            "active_tasks": {
                str(task_id): dict(state.active_tasks[task_id])
                for task_id in sorted(state.active_tasks)
            },
            "next_task_id": state.next_task_id,
        },
    )


def _require_group_message_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[
    PluginExecutionContext | None,
    Conversation | None,
    Message | None,
    PluginExecutionResult | None,
]:
    """交叉验证会话、消息、机器人、渠道和发送人，载荷不参与身份定位。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
    ):
        return None, None, None, _error_result("请在群聊中使用游戏任务。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    if (
        conversation is None
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
        return None, None, None, _error_result("当前群聊消息上下文无效。", "trusted_context_required")
    return context, conversation, message, None


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
    """定时动作只接受任务系统重建的绑定和目标会话。"""

    if context is None or context.channel_id is None or context.conversation_id is None:
        return None, None, None, None, _error_result("随机发题缺少目标群聊。", "trusted_context_required")
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
        return None, None, None, None, _error_result("随机发题上下文无效。", "trusted_context_required")
    return context, binding, conversation, channel, None


def _load_config(raw_config: Mapping[str, Any]) -> GameTaskConfig:
    raw = dict(raw_config)
    return GameTaskConfig(
        score_multiplier=_bounded_int(raw.get("score_multiplier"), default=1, minimum=1, maximum=10),
        ranking_limit=_bounded_int(raw.get("ranking_limit"), default=10, minimum=3, maximum=20),
        max_active_tasks=_bounded_int(raw.get("max_active_tasks"), default=10, minimum=1, maximum=MAX_STORED_ACTIVE_TASKS),
        random_task_enabled=_as_bool(raw.get("random_task_enabled"), default=False),
        random_task_time=_single_line(raw.get("random_task_time") or "17:58", limit=5),
        random_task_conversation_ids=_positive_ids(raw.get("random_task_conversation_ids")),
    )


def _parse_command(content: str) -> tuple[str, str]:
    normalized = str(content or "").strip()
    if normalized[:1] in {"/", "#"}:
        normalized = normalized[1:].lstrip()
    parts = normalized.split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].strip().lower(), parts[1].strip() if len(parts) == 2 else ""


def _normalize_answer(value: Any) -> str:
    return re.sub(r"[\s\-—_·,，.。!！?？:：()（）\[\]【】]+", "", str(value or "").strip().lower())


def _state_key(context: PluginExecutionContext) -> str:
    return f"{STATE_KEY_PREFIX}:{int(context.conversation_id or 0)}"


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


def _scheduled_skip_result(message: str, provider_status: str) -> PluginExecutionResult:
    """定时任务没有业务目标时视为正常跳过，避免 Worker 进行无意义重试。"""

    return PluginExecutionResult(
        ok=True,
        status="skipped",
        message=message,
        output={"provider_status": provider_status},
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


def _positive_ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    resolved: set[int] = set()
    for item in value:
        try:
            current = int(item)
        except (TypeError, ValueError):
            continue
        if current > 0:
            resolved.add(current)
    return tuple(sorted(resolved))
