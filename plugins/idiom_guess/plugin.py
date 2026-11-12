from __future__ import annotations

import base64
import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import replace
from datetime import timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, Message, PluginStateRecord
from app.services.account_identity import account_identity
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.member_names import member_names
from app.services.plugin_runtime.replies import image_reply, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore, parse_state_payload


CODE = "idiom_guess"
ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
PREFIX = re.compile(r"^(?:【?答案】?|答[：:])\s*[：:]?\s*")
DEFAULT_KEYWORDS = ("猜成语", "看图猜成语")


def normalize_answer_text(value: object) -> str:
    text = PREFIX.sub("", unicodedata.normalize("NFKC", str(value or "").strip()))
    return "".join(char for char in text if not char.isspace()
                   and not unicodedata.category(char).startswith("Z")
                   and unicodedata.category(char) != "Cf"
                   and char not in "：:，,。.！!？?【】[]")


@lru_cache(maxsize=1)
def _questions() -> tuple[dict[str, str], ...]:
    with (ROOT / "assets/questions.tsv").open(encoding="utf-8-sig", newline="") as stream:
        questions = tuple(dict(row) for row in csv.DictReader(stream, delimiter="\t"))
    if not questions or any(not row.get("cy") or not row.get("pid")
                            or Path(row["pid"]).name != row["pid"]
                            or not (ROOT / "assets/images" / row["pid"]).is_file() for row in questions):
        raise ValueError("猜成语题库不完整或图片缺失")
    return questions


def _int(config: Mapping[str, Any], key: str, default: int, maximum: int) -> int:
    try:
        return max(0, min(int(config.get(key, default)), maximum))
    except (ValueError, TypeError):
        return default


def _keywords(config: Mapping[str, Any]) -> tuple[str, ...]:
    value = config.get("trigger_keywords", DEFAULT_KEYWORDS)
    if isinstance(value, str):
        value = value.replace("，", ",").split(",")
    return tuple(str(item).strip() for item in value if str(item).strip()) if isinstance(value, (list, tuple)) and value else DEFAULT_KEYWORDS


def _enabled(value: object) -> bool:
    return value is True or str(value).lower() in {"true", "1", "on", "yes"}


def _room_key(channel_id: int, external_id: str) -> str:
    return f"room:{channel_id}:{hashlib.sha256(external_id.encode('utf-8')).hexdigest()[:32]}"


def _record(db: Session, context: PluginExecutionContext, key: str, *, lock: bool = False,
            namespace: str = CODE) -> PluginStateRecord | None:
    statement = select(PluginStateRecord).where(
        PluginStateRecord.tenant_id == context.tenant_id,
        PluginStateRecord.bot_plugin_id == context.binding_id,
        PluginStateRecord.namespace == namespace,
        PluginStateRecord.state_key == key,
    ).execution_options(populate_existing=True)
    return db.scalar(statement.with_for_update() if lock else statement)


def _lock_binding(db: Session, context: PluginExecutionContext) -> dict[str, Any]:
    """用现有状态行串行化同一绑定的出题和抢答，多个 Worker 也只能结算一次。"""
    row = _record(db, context, "cursor")
    if row is None:
        try:
            with db.begin_nested():
                db.add(PluginStateRecord(tenant_id=context.tenant_id, bot_plugin_id=context.binding_id,
                                        namespace=CODE, state_key="cursor", payload_json='{"position":0}'))
                db.flush()
        except IntegrityError:
            pass
    # SQLite 的 FOR UPDATE 不加锁，先写该行；其他数据库由同一条 UPDATE 获得行锁。
    db.execute(update(PluginStateRecord).where(
        PluginStateRecord.tenant_id == context.tenant_id,
        PluginStateRecord.bot_plugin_id == context.binding_id,
        PluginStateRecord.namespace == CODE, PluginStateRecord.state_key == "cursor",
    ).values(updated_at=now_shanghai()).execution_options(synchronize_session=False))
    return parse_state_payload(_record(db, context, "cursor", lock=True).payload_json)


def _lock_request(db: Session, context: PluginExecutionContext | None) -> None:
    if context is not None and context.tenant_id == require_tenant_id():
        # SQLite 保存点内先读再写会出现锁升级冲突；先锁已有绑定，再进入可回滚业务保存点。
        # 不改变绑定内容，同时让多进程和其他数据库复用同一把数据库行锁。
        db.execute(update(BotPlugin).where(
            BotPlugin.id == context.binding_id, BotPlugin.tenant_id == context.tenant_id,
            BotPlugin.bot_id == context.bot_id,
        ).values(updated_at=BotPlugin.updated_at).execution_options(synchronize_session=False))


def _get(db: Session, context: PluginExecutionContext, key: str) -> dict[str, Any]:
    row = _record(db, context, key, lock=True)
    return parse_state_payload(row.payload_json) if row else {}


def _active(state: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    if state.get("status") != "active":
        return False
    minutes = _int(config, "game_timeout_minutes", 30, 1440)
    return not minutes or float(state.get("started_at", 0)) + minutes * 60 > now_shanghai().timestamp()


def _result(status: str, message: str = "", replies: list[dict[str, Any]] | None = None) -> PluginExecutionResult:
    return PluginExecutionResult(ok=True, status="completed" if replies or status == "daily_started" else "skipped",
                                 message=message or "猜成语未产生回复。",
                                 output={"provider_status": status, "replies": replies or []})


def _resolve(db: Session, context: PluginExecutionContext | None, config: Mapping[str, Any]):
    if context is None or context.tenant_id != require_tenant_id():
        return None
    binding = db.get(BotPlugin, context.binding_id)
    conversation = db.get(Conversation, context.conversation_id) if context.conversation_id else None
    channel = db.get(Channel, context.channel_id) if context.channel_id else None
    if (not binding or binding.tenant_id != context.tenant_id or binding.bot_id != context.bot_id
            or not binding.plugin or binding.plugin.code != CODE or binding.status != "enabled"
            or not binding.bot or not binding.bot.is_enabled or binding.bot.deleted_at is not None
            or not conversation or conversation.tenant_id != context.tenant_id or conversation.bot_id != context.bot_id
            or conversation.channel_id != context.channel_id or conversation.chat_type != "group"
            or not channel or channel.tenant_id != context.tenant_id or channel.bot_id != context.bot_id
            or not channel.is_enabled or channel.deleted_at is not None):
        return None
    from app.services.plugin_directory import build_static_dispatch_plan

    room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id,
                                           Chatroom.external_id == conversation.external_id))
    if room is None or not room.is_active or room.status == "left" or room.management_status != "enabled":
        return None
    event = {"conversation": {"chat_type": "group", "external_id": conversation.external_id},
             "message": {"message_type": "text", "sender_id": context.sender_id or "", "content": ""}}
    plan = build_static_dispatch_plan(db, channel, event)
    candidate = next((item for item in plan.candidates if item.binding_id == binding.id), None)
    if candidate is None:
        return None
    effective = dict(config)
    if candidate.scope_config_override_json:
        effective.update(json.loads(candidate.scope_config_override_json))
    return binding, conversation, channel, effective


def matches_event(context) -> bool:
    """在匹配阶段排除无关聊天，避免抢占框架的 AI 兜底。"""
    if context.chat_type != "group" or context.message_type != "text":
        return False
    with tenant_session() as db:
        binding = db.get(BotPlugin, context.binding_id)
        if not binding or binding.tenant_id != require_tenant_id():
            return False
        conversation = db.scalar(select(Conversation).where(
            Conversation.bot_id == context.bot_id, Conversation.channel_id == context.channel_id,
            Conversation.external_id == context.conversation_external_id, Conversation.chat_type == "group",
        ).order_by(Conversation.id))
        if conversation is None:
            return False
        message_context = PluginExecutionContext(tenant_id=binding.tenant_id, binding_id=binding.id,
            bot_id=binding.bot_id, channel_id=context.channel_id, conversation_id=conversation.id)
        # 自发消息不参与游戏，沿用框架已有可信消息归属。
        channel = db.get(Channel, context.channel_id)
        if channel and context.sender_id and context.sender_id == account_identity(channel)["external_id"]:
            return False
        if context.content.strip() in _keywords(context.binding_config):
            return True
        row = _record(db, message_context, _room_key(context.channel_id, conversation.external_id))
        state = parse_state_payload(row.payload_json) if row else {}
        if not _active(state, context.binding_config):
            return False
        question = _questions()[int(state["position"]) % len(_questions())]
        return normalize_answer_text(context.content) == normalize_answer_text(question["cy"])


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action == "reload":
        return PluginExecutionResult(True, "running", f"看图猜成语已就绪，共 {len(_questions())} 道题。")
    return PluginExecutionResult(False, "error", "请在已启用插件的群聊发送开局关键词。")


def _start(db: Session, context: PluginExecutionContext, conversation: Conversation,
           config: Mapping[str, Any], cursor: dict[str, Any], *, position: int | None = None,
           daily_date: str = "") -> PluginExecutionResult:
    store = PluginStateStore(db, context)
    key = _room_key(context.channel_id, conversation.external_id)
    state = _get(db, context, key)
    if daily_date and state.get("daily_date") == daily_date:
        return _result("already_started")
    if not daily_date and _active(state, config):
        return _result("active_game_exists")
    receipt = f"start:{context.message_id}" if context.message_id else ""
    if receipt and _get(db, context, receipt):
        return _result("duplicate_message")
    questions = _questions()
    selected = int(cursor.get("position", 0)) % len(questions) if position is None else position % len(questions)
    question = questions[selected]
    if conversation.channel_type == "wechat_webridge":
        from app.core.settings import settings

        if not str(settings.public_base_url or "").strip():
            return PluginExecutionResult(False, "error", "微桥发图需要配置手机可访问的 PUBLIC_BASE_URL。")
    picture = base64.b64encode((ROOT / "assets/images" / question["pid"]).read_bytes()).decode("ascii")
    if position is None:
        cursor["position"] = (selected + 1) % len(questions)
        store.put(CODE, "cursor", cursor)
    state = {"position": selected, "status": "active", "started_at": now_shanghai().timestamp(),
             "daily_date": daily_date or state.get("daily_date", "")}
    store.put(CODE, key, state)
    if receipt:
        store.put(CODE, receipt, {"started": True}, ttl_seconds=7 * 86400)
    return _result("started", "猜成语已开局。", [image_reply(base64_data=picture)])


def _leaderboard(db: Session, context: PluginExecutionContext, conversation: Conversation, limit: int) -> str:
    namespace = f"idiom_scores_{context.channel_id}_{_room_key(context.channel_id, conversation.external_id).split(':')[-1]}"
    rows = db.scalars(select(PluginStateRecord).where(
        PluginStateRecord.tenant_id == context.tenant_id, PluginStateRecord.bot_plugin_id == context.binding_id,
        PluginStateRecord.namespace == namespace).with_for_update().execution_options(populate_existing=True)).all()
    scores = [parse_state_payload(row.payload_json) for row in rows]
    # 记分时存的是当时的名字(微桥常给不出,会是 wxid);显示时按用户 ID 实时换成本群群昵称。
    # 老记录没有 user_id,继续用当时存的名字,那位成员下次答对后会带上 user_id。
    ids = [str(item["user_id"]) for item in scores if item.get("user_id")]
    names = member_names(db, channel_id=int(context.channel_id), group_external_id=str(conversation.external_id), user_ids=ids,
                         fallbacks={str(item["user_id"]): str(item.get("name") or "") for item in scores if item.get("user_id")})
    for item in scores:
        if item.get("user_id"):
            item["name"] = names.get(str(item["user_id"]), item["name"])
    scores.sort(key=lambda item: (-int(item["count"]), float(item["last_correct_at"]), item["name"]))
    lines = ["【本群猜成语排行榜】"]
    lines.extend(f"{index}. {item['name']} - {item['count']}题" for index, item in enumerate(scores[:limit], 1))
    return "\n".join(lines)


def _queue_reply(db: Session, binding: BotPlugin, conversation: Conversation, channel: Channel,
                 reply: dict[str, Any], *, available_at=None):
    payload = {"external_id": conversation.external_id, "receiver_id": conversation.external_id,
               "chat_type": "group", "message_type": reply["type"]}
    payload.update({key: value for key, value in reply.items() if key != "type"})
    if channel.channel_type == "wechat_webridge":
        from app.core.settings import settings
        from app.services.outbound_files import externalize_media_payload
        from app.services.task_queue import OUTBOUND_DISPATCH_JOB, enqueue_job

        # 先由出站 Worker 调用统一适配器，才转成手机命令。这样延迟、游戏状态和发题任务
        # 可以一起提交，避免适配器内部 commit 提前释放抢答锁，或榜单被手机提前领走。
        payload = externalize_media_payload(payload, tenant_id=binding.tenant_id,
                                            channel_id=channel.id, public_base_url=settings.public_base_url)
        if payload.get("base64"):
            raise ValueError("定时发图需要配置手机可访问的 PUBLIC_BASE_URL")
        return enqueue_job(db, job_type=OUTBOUND_DISPATCH_JOB,
                           payload={"channel_id": channel.id, "conversation_id": conversation.id,
                                    "payload": payload, "plugin_code": CODE},
                           related_type="idiom_guess_binding", related_id=binding.id,
                           # 转换器会提交手机命令，自动重试可能造成重复发图或重复榜单。
                           priority=60, available_at=available_at, max_attempts=1)
    return enqueue_outbound_job(db, conversation=conversation, channel=channel, payload=payload, plugin_code=CODE,
                                related_type="idiom_guess_binding", related_id=binding.id,
                                priority=60, available_at=available_at)


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    # Worker 记录失败任务时仍会提交外层会话；保存点保证入队失败不会留下半次计分。
    _lock_request(db, request.context)
    with db.begin_nested():
        return _execute_message_action(db, request)


def _execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action != "execute":
        return execute_action(request)
    resolved = _resolve(db, request.context, request.config)
    if resolved is None:
        return _result("skipped")
    binding, conversation, channel, config = resolved
    context = request.context
    message = db.get(Message, context.message_id) if context.message_id else None
    if (not message or message.tenant_id != context.tenant_id or message.conversation_id != conversation.id
            or message.sender_id != context.sender_id or not context.sender_id or message.direction != "in"
            or message.message_type != "text" or context.sender_id == account_identity(channel)["external_id"]):
        return _result("skipped")
    cursor = _lock_binding(db, context)
    if message.content.strip() in _keywords(config):
        return _start(db, context, conversation, config, cursor)
    key = _room_key(channel.id, conversation.external_id)
    state = _get(db, context, key)
    if not _active(state, config):
        return _result("skipped")
    question = _questions()[int(state["position"]) % len(_questions())]
    if normalize_answer_text(message.content) != normalize_answer_text(question["cy"]):
        return _result("wrong_answer")
    store = PluginStateStore(db, context)
    receipt = f"answer:{message.id}"
    if _get(db, context, receipt):
        return _result("duplicate_message")
    state.update(status="answered", winner=context.sender_id)
    store.put(CODE, key, state)
    store.put(CODE, receipt, {"answered": True}, ttl_seconds=7 * 86400)
    # 每位用户单独保存题数，群人数增长不会撑满单条 64 KiB 状态。
    namespace = f"idiom_scores_{channel.id}_{key.split(':')[-1]}"
    score_key = hashlib.sha256(context.sender_id.encode('utf-8')).hexdigest()
    previous = _record(db, context, score_key, lock=True, namespace=namespace)
    score = parse_state_payload(previous.payload_json) if previous else {"count": 0}
    name = str(message.sender_name or "").strip()
    if not name or name == context.sender_id:
        name = str(score.get("name") or context.sender_id)
    name = name[:255]
    score.update(count=int(score["count"]) + 1, name=name, user_id=context.sender_id, last_correct_at=now_shanghai().timestamp())
    store.put(namespace, score_key, score)
    explanation = "\n".join(f"【{label}】{question.get(field, '')}" for label, field in
                              [("答案", "cy"), ("发音", "fayin"), ("解释", "jieshi"), ("出处", "chuchu"), ("造句", "zaoju")])
    replies = [text_reply(explanation)]
    board = text_reply(_leaderboard(db, context, conversation, max(1, _int(config, "leaderboard_limit", 5, 50))))
    delay = _int(config, "leaderboard_delay_seconds", 1, 60)
    if delay:
        _queue_reply(db, binding, conversation, channel, board, available_at=now_shanghai() + timedelta(seconds=delay))
    else:
        replies.append(board)
    return _result("correct", "猜成语回答正确。", replies)


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    # 同一日的群游戏、游标和出站任务全部成功后才一起保留。
    _lock_request(db, request.context)
    with db.begin_nested():
        return _execute_scheduled_action(db, request)


def _execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action != "idiom_daily_start" or not request.context or not _enabled(request.config.get("scheduled_enabled")):
        return _result("skipped")
    context = request.context
    binding = db.get(BotPlugin, context.binding_id)
    if (not binding or binding.tenant_id != require_tenant_id() or binding.tenant_id != context.tenant_id
            or binding.bot_id != context.bot_id or binding.status != "enabled"
            or not binding.bot or not binding.bot.is_enabled):
        return _result("skipped")
    today = now_shanghai().astimezone(BEIJING).date().isoformat()
    # 一天只开一局由下面的 daily_date 游标保证;不依赖调度器传参(声明式 [[schedule]] 不带 date)。
    cursor = _lock_binding(db, context)
    store = PluginStateStore(db, context)
    if cursor.get("daily_date") == today:
        return _result("already_started")
    targets = []
    rooms = db.scalars(select(Chatroom).where(Chatroom.bot_id == binding.bot_id, Chatroom.is_active.is_(True),
                                             Chatroom.management_status == "enabled").order_by(Chatroom.id)).all()
    for room in rooms:
        conversation = db.scalar(select(Conversation).where(
            Conversation.bot_id == binding.bot_id, Conversation.channel_id == room.channel_id,
            Conversation.external_id == room.external_id, Conversation.chat_type == "group").order_by(Conversation.id))
        if conversation is None:
            channel = db.get(Channel, room.channel_id)
            if not channel or not channel.is_enabled or channel.deleted_at is not None:
                continue
            # 已同步的受管群即使还没收到消息，也能参加每天开局。
            conversation = Conversation(tenant_id=context.tenant_id, bot_id=binding.bot_id, channel_id=channel.id,
                                        channel_type=channel.channel_type, external_id=room.external_id,
                                        title=room.name, chat_type="group")
            db.add(conversation)
            db.flush()
        group_context = replace(context, channel_id=conversation.channel_id, conversation_id=conversation.id)
        resolved = _resolve(db, group_context, request.config)
        if resolved and _enabled(resolved[3].get("scheduled_enabled")):
            targets.append((group_context, resolved))
    if not targets:
        return _result("skipped")
    position = int(cursor.get("position", 0)) % len(_questions())
    for group_context, (_, conversation, channel, config) in targets:
        result = _start(db, group_context, conversation, config, cursor, position=position, daily_date=today)
        if not result.ok:
            raise ValueError(result.message)
        for reply in result.output["replies"]:
            _queue_reply(db, binding, conversation, channel, reply)
    cursor.update(position=(position + 1) % len(_questions()), daily_date=today)
    store.put(CODE, "cursor", cursor)
    return _result("daily_started", f"已为 {len(targets)} 个群创建猜成语发图任务。")
