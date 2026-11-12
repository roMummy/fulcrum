"""Durable multi-round game; no per-question counters or weighted sampling.

Ported gameplay from the MIT-licensed koishi-plugin-guess-image.
Timers and sends use Fulcrum's existing transactional queue, never plugin threads.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, update

from app.core.tenant_context import require_tenant_id
from app.core.tenant_db import tenant_session
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message, PluginStateRecord, RawWebhookEvent, TaskJob
from app.models.guess_image import GuessImageQuestion
from app.services.account_identity import account_identity
from app.services.guess_image_bank import asset_path
from app.services.identity_directory import looks_like_raw_id
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionResult
from app.services.plugin_runtime.member_names import member_names
from app.services.plugin_runtime.scheduler import enqueue_plugin_scheduled_action
from app.services.plugin_runtime.state_store import PluginStateStore, parse_state_payload

CODE = "guess_image"
STARTS = {"猜图片", "guess-image"}
LIMIT = re.compile(r"^(?:猜图片\s*次数|guess-image\s+次数)(?:\s+(默认|\d+))?$")
SEND_TIMEOUT = 120
BEIJING = timezone(timedelta(hours=8))


def _int(config, key, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(config.get(key, default))))
    except (ValueError, TypeError):
        return default


def _key(channel_id, external_id):
    return f"room:{channel_id}:{hashlib.sha256(external_id.encode()).hexdigest()[:32]}"


def _row(db, context, key):
    return db.scalar(select(PluginStateRecord).where(
        PluginStateRecord.tenant_id == context.tenant_id,
        PluginStateRecord.bot_plugin_id == context.binding_id, PluginStateRecord.namespace == CODE,
        PluginStateRecord.state_key == key).execution_options(populate_existing=True))


def _state(db, context, key):
    row = _row(db, context, key)
    return parse_state_payload(row.payload_json) if row else {}


def _lock(db, context):
    # First operation is a write: SQLite cannot safely upgrade a deferred read transaction.
    if context and context.tenant_id == require_tenant_id():
        db.execute(update(BotPlugin).where(BotPlugin.id == context.binding_id,
            BotPlugin.tenant_id == context.tenant_id, BotPlugin.bot_id == context.bot_id)
            .values(updated_at=BotPlugin.updated_at).execution_options(synchronize_session=False))


def _result(reason, text=""):
    return PluginExecutionResult(True, "completed", text or "猜图片状态已处理", {"provider_status": reason})


def _resolve(db, context, config):
    if not context or context.tenant_id != require_tenant_id():
        return None
    binding = db.get(BotPlugin, context.binding_id, populate_existing=True)
    conversation = db.get(Conversation, context.conversation_id, populate_existing=True) if context.conversation_id else None
    channel = db.get(Channel, context.channel_id, populate_existing=True) if context.channel_id else None
    if (not binding or binding.tenant_id != context.tenant_id or binding.bot_id != context.bot_id or binding.status != "enabled"
        or not binding.plugin or binding.plugin.code != CODE or binding.plugin.missing_at is not None
        or not binding.bot or not binding.bot.is_enabled or binding.bot.deleted_at is not None
        or not conversation or conversation.tenant_id != context.tenant_id or conversation.bot_id != binding.bot_id or conversation.chat_type != "group"
        or conversation.channel_id != context.channel_id or not channel or channel.tenant_id != context.tenant_id or channel.bot_id != binding.bot_id
        or not channel.is_enabled or channel.deleted_at is not None):
        return None
    from app.services.plugin_governance import blocked_plugin_codes
    from app.services.plugin_directory import build_static_dispatch_plan
    if CODE in blocked_plugin_codes(db, context.tenant_id):
        return None
    room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id))
    if not room or not room.is_active or room.status == "left" or room.management_status != "enabled":
        return None
    plan = build_static_dispatch_plan(db, channel, {"conversation": {"chat_type": "group", "external_id": conversation.external_id},
                                                  "message": {"message_type": "text", "content": ""}})
    candidate = next((item for item in plan.candidates if item.binding_id == binding.id), None)
    if candidate is None:
        return None
    effective = dict(config)
    if candidate.scope_config_override_json:
        effective.update(json.loads(candidate.scope_config_override_json))
    return binding, conversation, channel, room, effective


def _plain(event, attachments=()):
    if attachments:
        return False
    event = event if isinstance(event, dict) else {}
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if message.get("message_type", "text") != "text":
        return False
    # Providers preserve either normalized quote metadata, XML or the original envelope.
    for item in (details, message, event, event.get("body") or {}):
        if not isinstance(item, dict):
            continue
        if any(item.get(k) for k in ("quote_payload", "quote_message_id", "quoted", "quote", "refermsg", "reply_to_message", "attachments", "media")):
            return False
        if item.get("mentioned_self") or item.get("is_at") or item.get("from_self") or item.get("is_self_sent"):
            return False
        if any(item.get(k) for k in ("content_xml", "xml")):
            return False
        elements = item.get("elements")
        if elements and any(not isinstance(e, dict) or e.get("type") != "text" for e in elements):
            return False
    return True


def _original_text(original, fallback, sender_id):
    """Use the saved envelope when an adapter trims its display text."""
    containers = [original.get("message"), original.get("body"), original.get("Data"),
                  original.get("data"), original.get("msg"), original]
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("Content", "content", "Text", "text"):
            value = container.get(key)
            if isinstance(value, dict):
                value = value.get("string", value.get("str"))
            if isinstance(value, str):
                # iPad prefixes group text with the actual sender, followed by a newline.
                prefix = f"{sender_id}:\n"
                return value[len(prefix):] if sender_id and value.startswith(prefix) else value
    return fallback


def matches_event(context):
    if context.chat_type != "group" or context.message_type != "text" or not _plain(context.normalized_event, context.attachments):
        return False
    if context.content.strip() in STARTS or LIMIT.fullmatch(context.content.strip()):
        return True
    with tenant_session() as db:
        conversation = db.scalar(select(Conversation).where(Conversation.channel_id == context.channel_id,
            Conversation.bot_id == context.bot_id, Conversation.external_id == context.conversation_external_id,
            Conversation.chat_type == "group").order_by(Conversation.id))
        if not conversation:
            return False
        channel = db.get(Channel, context.channel_id)
        if not channel or context.sender_id == account_identity(channel)["external_id"]:
            return False
        state = _state(db, context, _key(channel.id, conversation.external_id))
        game = state.get("game") or {}
        raw = db.get(RawWebhookEvent, context.raw_event_id) if context.raw_event_id else None
        original = parse_state_payload(raw.payload_json) if raw else {}
        content = _original_text(original, context.content, context.sender_id)
        return bool(game.get("status") == "open" and now_shanghai().timestamp() < game.get("deadline", 0)
                    and _plain(original) and content == game["questions"][game["round"]]["answer"])


def _queue(db, binding, conversation, channel, game, label, *, text=None, image=None):
    payload = {"external_id": conversation.external_id, "receiver_id": conversation.external_id,
               "chat_type": "group", "message_type": "image" if image else "text"}
    if image:
        payload["base64"] = base64.b64encode(asset_path(image).read_bytes()).decode("ascii")
    else:
        payload["content"] = text
    dedupe = f"gi:{binding.id}:{game['id']}:{game['round']}:{label}"
    if channel.channel_type == "wechat_webridge":
        from app.core.settings import settings
        from app.services.outbound_files import externalize_media_payload
        from app.services.task_queue import OUTBOUND_DISPATCH_JOB, enqueue_job
        payload = externalize_media_payload(payload, tenant_id=binding.tenant_id, channel_id=channel.id,
                                            public_base_url=settings.public_base_url)
        if payload.get("base64"):
            raise ValueError("微桥猜图片需要配置手机可访问的 PUBLIC_BASE_URL")
        job = enqueue_job(db, job_type=OUTBOUND_DISPATCH_JOB, payload={"channel_id": channel.id,
            "conversation_id": conversation.id, "payload": payload, "plugin_code": CODE}, priority=60,
            related_type=CODE, related_id=binding.id, dedupe_key=dedupe, max_attempts=1)
    else:
        job = enqueue_outbound_job(db, conversation=conversation, channel=channel, payload=payload, plugin_code=CODE,
            related_type=CODE, related_id=binding.id, priority=60, dedupe_key=dedupe, max_attempts=1)
    game.setdefault("outbound", []).append(job.id)
    return job.id


def _schedule(db, resolved, game, kind, timestamp):
    binding, conversation = resolved[:2]
    enqueue_plugin_scheduled_action(db, binding, action_key=f"gi.{kind}.{game['id'][:8]}.{game['round']}",
        scheduled_for=datetime.fromtimestamp(timestamp, now_shanghai().tzinfo),
        parameters={"game": game["id"], "round": game["round"], "kind": kind}, conversation_id=conversation.id)


def _persist(db, context, key, state):
    PluginStateStore(db, context).put(CODE, key, state)


def _cancel_pending(db, game):
    for job_id in game.get("outbound", []):
        db.execute(update(TaskJob).where(TaskJob.id == job_id, TaskJob.status == "pending")
                   .values(status="cancelled", finished_at=now_shanghai()).execution_options(synchronize_session=False))
        job = db.get(TaskJob, job_id, populate_existing=True)
        # A WeBridge dispatch job can be complete while its phone command is pending.
        result = parse_state_payload(job.result_json or "{}") if job else {}
        command_id = (result.get("adapter_result") or {}).get("job_id")
        if command_id:
            db.execute(update(TaskJob).where(TaskJob.id == command_id, TaskJob.status == "pending")
                       .values(status="cancelled", finished_at=now_shanghai()).execution_options(synchronize_session=False))


def _finish(db, resolved, state, reason):
    binding, conversation, channel = resolved[:3]
    game = state["game"]
    game["status"] = "finished"
    scores = game.get("scores", [])
    scores.sort(key=lambda item: (-item["score"], item["first"]))
    names = member_names(db, channel_id=channel.id, group_external_id=conversation.external_id,
        user_ids=[item["user"] for item in scores], fallbacks={item["user"]: item.get("name", "") for item in scores})
    lines = [f"猜图片结束：{reason}", "【本局排行榜】"]
    for index, score in enumerate(scores, 1):
        name = names[score["user"]]
        if not name or looks_like_raw_id(name) or name == score["user"]:
            name = f"玩家{score['first'] + 1}"
        name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))[:40]
        lines.append(f"{index}. {name or '玩家'}：{score['score']} 分")
    if not scores:
        lines.append("本局暂无人答对。")
    _queue(db, binding, conversation, channel, game, "finish", text="\n".join(lines))
    # Keep daily quota and only this small terminal marker; no question usage statistics.
    game.pop("questions", None)
    game.pop("scores", None)


def _refund(state):
    game = state["game"]
    if game.get("charged") and not game.get("started") and state.get("day") == game.get("charged_day"):
        state["used"] = max(0, state.get("used", 0) - 1)
    game["charged"] = False


def _send_round(db, resolved, state):
    binding, conversation, channel = resolved[:3]
    game = state["game"]
    game.update(status="sending", sending_at=now_shanghai().timestamp(), outbound=[], hinted=False)
    question = game["questions"][game["round"]]
    _queue(db, binding, conversation, channel, game, "round", text=f"猜图片 第 {game['round'] + 1}/{len(game['questions'])} 题 · {game['seconds']} 秒")
    game["image_job"] = _queue(db, binding, conversation, channel, game, "image", image=question["asset_id"])
    _schedule(db, resolved, game, "delivery", now_shanghai().timestamp() + 1)


def _advance(db, resolved, state):
    game = state["game"]
    game["round"] += 1
    if game["round"] >= len(game["questions"]):
        _finish(db, resolved, state, "全部题目已完成")
    else:
        _send_round(db, resolved, state)


def _day(state):
    day = now_shanghai().astimezone(BEIJING).date().isoformat()
    if state.get("day") != day:
        state.update(day=day, used=0)


def _limit(db, resolved, context, state, match):
    binding, conversation, channel, room, config = resolved
    value = match.group(1)
    if value is not None:
        member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id,
            ChatroomMember.member_external_id == context.sender_id, ChatroomMember.status == "active"))
        if context.sender_id != room.owner_external_id and (not member or member.role not in {"owner", "admin"}):
            text = "只有本群群主或群管理员可以修改次数上限。"
        elif value != "默认" and int(value) > 10000:
            text = "每日上限应在 0–10000 之间，0 表示不限次。"
        else:
            state["limit"] = None if value == "默认" else int(value)
            text = "已更新本群每日开局上限。\n"
    else:
        text = ""
    limit = state.get("limit")
    limit = _int(config, "daily_limit", 5, 0, 10000) if limit is None else limit
    text += f"今日已用 {state.get('used', 0)} 次；每日上限：{'不限' if not limit else limit}（北京时间零点重置）。"
    _queue(db, binding, conversation, channel, {"id": f"limit{context.message_id}", "round": 0}, "reply", text=text)


def execute_action(request):
    return _result("ready", "请在已启用插件的群聊发送“猜图片”，题目在租户的“管理题库”中维护。")


def execute_message_action(db, request):
    _lock(db, request.context)
    with db.begin_nested():
        return _execute_message(db, request)


def _execute_message(db, request):
    context = request.context
    resolved = _resolve(db, context, request.config)
    if not resolved:
        return _result("unavailable")
    binding, conversation, channel, room, config = resolved
    message = db.get(Message, context.message_id) if context.message_id else None
    if (not message or message.direction != "in" or message.conversation_id != conversation.id
        or message.sender_id != context.sender_id or not context.sender_id or message.message_type != "text"
        or context.sender_id == account_identity(channel)["external_id"]):
        return _result("ignored")
    event = request.payload.get("normalized_event") or {}
    # Read the trusted persisted envelope as well: caller-supplied fields cannot turn a quote into an answer.
    raw = db.get(RawWebhookEvent, context.raw_event_id) if context.raw_event_id else None
    original = parse_state_payload(raw.payload_json) if raw else {}
    if not _plain(event, request.payload.get("attachments", ())) or not _plain(original):
        return _result("not_plain_text")
    key = _key(channel.id, conversation.external_id)
    state = _state(db, context, key)
    _day(state)
    receipt = f"message:{message.id}"
    if _state(db, context, receipt):
        return _result("duplicate")
    PluginStateStore(db, context).put(CODE, receipt, {"handled": True}, ttl_seconds=86400)
    content = _original_text(original, message.content, context.sender_id)
    match = LIMIT.fullmatch(content.strip())
    game = state.get("game") or {}
    # A disabled binding's timers are intentionally rejected by the framework. On re-enable,
    # retire overdue games instead of leaving a group permanently stuck in an old round.
    expiry = game.get("deadline", 0) if game.get("status") == "open" else game.get("sending_at", 0)
    if game.get("status") in {"open", "sending"} and now_shanghai().timestamp() > expiry + SEND_TIMEOUT:
        _cancel_pending(db, game)
        _refund(state)
        game["status"] = "finished"
    if match:
        _limit(db, resolved, context, state, match)
    elif content.strip() in STARTS:
        if game.get("status") in {"open", "sending"}:
            _queue(db, binding, conversation, channel, {"id": f"busy{message.id}", "round": 0}, "reply", text="本群已有一局猜图片正在进行。")
        else:
            limit = state.get("limit")
            limit = _int(config, "daily_limit", 5, 0, 10000) if limit is None else limit
            questions = db.scalars(select(GuessImageQuestion).where(GuessImageQuestion.status == "enabled",
                *([GuessImageQuestion.category == str(config["category"])] if config.get("category") else []))).all()
            count = _int(config, "rounds", 5, 1, 20)
            error = "今日开局次数已用完。" if limit and state["used"] >= limit else ""
            if len(questions) < count:
                error = f"可用题目不足 {count} 道，请先在管理题库中添加并启用题目。"
            if error:
                _queue(db, binding, conversation, channel, {"id": f"error{message.id}", "round": 0}, "reply", text=error)
            else:
                selected = random.SystemRandom().sample(questions, count)
                # Validate every resource before reserving quota or queuing the first image.
                for question in selected:
                    asset_path(question.asset_id)
                state["game"] = {"id": uuid4().hex, "round": 0, "scores": [], "started": False,
                    "questions": [{"id": q.question_id, "asset_id": q.asset_id, "answer": q.answer} for q in selected],
                    "seconds": _int(config, "seconds", 60, 5, 600), "hint": _int(config, "hint_remaining", 30, 0, 599),
                    "charged": bool(limit), "charged_day": state["day"]}
                if limit:
                    state["used"] += 1
                _send_round(db, resolved, state)
    elif game.get("status") == "open" and now_shanghai().timestamp() < game["deadline"] and content == game["questions"][game["round"]]["answer"]:
        # Binding row lock claims the round before any side effects; only the first worker scores.
        _cancel_pending(db, game)
        score = next((s for s in game["scores"] if s["user"] == context.sender_id), None)
        if score is None:
            score = {"user": context.sender_id, "name": str(message.sender_name or "")[:128], "score": 0, "first": len(game["scores"])}
            game["scores"].append(score)
        score["score"] += 1
        _queue(db, binding, conversation, channel, game, "correct", text=f"回答正确！答案：{content}")
        _advance(db, resolved, state)
    _persist(db, context, key, state)
    return _result("handled")


def _delivered(db, game):
    job = db.get(TaskJob, game["image_job"])
    if not job or job.status in {"failed", "cancelled"}:
        return "failed"
    result = parse_state_payload(job.result_json or "{}")
    if result.get("skipped"):
        return "failed"
    message_id = result.get("message_id")
    message = db.get(Message, message_id) if message_id else None
    if message and message.status == "failed":
        return "failed"
    if message and message.status == "sent" and message.external_status not in {"pending", "queued"}:
        return "sent"
    return "pending"


def _activity(db, conversation, channel, game):
    # Count all persisted human message types, without matching them and suppressing AI fallback.
    identity = account_identity(channel)["external_id"]
    since = ensure_shanghai_datetime(datetime.fromtimestamp(game["opened_at"], timezone.utc))
    until = ensure_shanghai_datetime(datetime.fromtimestamp(game["deadline"], timezone.utc))
    if db.scalar(select(Message.id).where(Message.conversation_id == conversation.id, Message.direction == "in",
        Message.sender_id.is_not(None), Message.sender_id != "", Message.sender_id != identity,
        Message.created_at >= since, Message.created_at < until).limit(1)) is not None:
        return True
    # Some providers persist content-less media only as raw envelopes. Inspect those
    # through their normalizer, without capturing all messages in the plugin matcher.
    from app.services.channel_adapters import get_channel_adapter
    adapter = get_channel_adapter(channel)
    events = db.scalars(select(RawWebhookEvent).where(RawWebhookEvent.channel_id == channel.id,
        RawWebhookEvent.created_at >= since, RawWebhookEvent.created_at < until).order_by(RawWebhookEvent.id)).yield_per(100)
    for raw in events:
        normalized = adapter.normalize_inbound_payload(channel, parse_state_payload(raw.payload_json))
        room = normalized.get("conversation") or {}
        message = normalized.get("message") or {}
        details = normalized.get("details") or {}
        if (room.get("external_id") == conversation.external_id and message.get("sender_id")
            and message["sender_id"] != identity and not details.get("from_self") and not details.get("is_self_sent")
            and message.get("message_type") not in {"system", "event", "group_member_join", "group_member_leave", "group_member_nickname_change"}):
            return True
    return False


def execute_scheduled_action(db, request):
    _lock(db, request.context)
    with db.begin_nested():
        return _execute_timer(db, request)


def _execute_timer(db, request):
    context = request.context
    if not context or not request.action.startswith("gi."):
        return _result("ignored")
    conversation = db.get(Conversation, context.conversation_id) if context.conversation_id else None
    if not conversation:
        return _result("unavailable")
    key = _key(context.channel_id, conversation.external_id)
    state = _state(db, context, key)
    game = state.get("game") or {}
    if (game.get("id") != request.payload.get("game") or game.get("round") != request.payload.get("round")
        or game.get("status") == "finished"):
        return _result("stale_timer")
    resolved = _resolve(db, context, request.config)
    if not resolved:
        _cancel_pending(db, game)
        _refund(state)
        game["status"] = "finished"
        _persist(db, context, key, state)
        return _result("unavailable")
    binding, conversation, channel = resolved[:3]
    kind = request.payload.get("kind")
    now = now_shanghai().timestamp()
    if kind == "delivery" and game["status"] == "sending":
        status = _delivered(db, game)
        if status == "sent":
            game.update(status="open", started=True, opened_at=now, deadline=now + game["seconds"])
            game["outbound"] = []
            if 0 < game["hint"] < game["seconds"]:
                _schedule(db, resolved, game, "hint", game["deadline"] - game["hint"])
            _schedule(db, resolved, game, "timeout", game["deadline"])
        elif status == "failed" or now >= game["sending_at"] + SEND_TIMEOUT:
            _cancel_pending(db, game)
            _refund(state)
            _finish(db, resolved, state, "图片发送失败或未确认，已停止本局；首题未确认时返还次数")
        else:
            _schedule(db, resolved, game, "delivery", now + 1)
    elif kind == "hint" and game["status"] == "open" and now < game["deadline"] and not game.get("hinted"):
        if now < game["deadline"] - game["hint"]:
            _schedule(db, resolved, game, "hint", game["deadline"] - game["hint"] + 1)
        else:
            answer = game["questions"][game["round"]]["answer"]
            _queue(db, binding, conversation, channel, game, "hint", text=f"首字提示：{answer[0]}（共 {len(answer)} 字）")
            game["hinted"] = True
    elif kind == "timeout" and game["status"] == "open":
        if now < game["deadline"]:
            _schedule(db, resolved, game, "timeout", game["deadline"] + 1)
        else:
            _cancel_pending(db, game)
            _queue(db, binding, conversation, channel, game, "timeout", text=f"时间到！答案：{game['questions'][game['round']]['answer']}")
            if _activity(db, conversation, channel, game):
                _advance(db, resolved, state)
            else:
                _finish(db, resolved, state, "本题期间无人参与")
    _persist(db, context, key, state)
    return _result("timer_handled")
