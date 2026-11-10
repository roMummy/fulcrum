"""AI 意图任务的独立执行器：模型调用在专用小线程池里完成，不占用主 worker。

主 worker 是单线程，一次模型调用可能耗时数十秒；如果在主线程里同步等待，
期间所有插件调度、出站和媒体任务都会被拖住。这里把慢的网络调用隔离出去：
  1. 主 worker 只在 AI 线程池有空位时领取意图任务（背压，不会无限堆积线程）；
  2. 线程内分三阶段：短会话取快照 -> 无会话调用模型 -> 短会话落库与下发，
     网络等待期间不占用任何数据库连接。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.logging import get_logger
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Channel, TaskJob
from app.services import ai_chat, ai_dispatch, ai_memory
from app.services.task_queue import (
    AI_INTENT_JOB,
    claim_next_job,
    complete_job,
    fail_job,
    parse_job_payload,
)

logger = get_logger(__name__)

# 同时在途的模型调用数上限：既限制对模型服务的并发，也限制线程和内存占用。
MAX_CONCURRENT_INTENTS = 4

_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_INTENTS, thread_name_prefix="ai-intent")
_in_flight = 0
_in_flight_lock = threading.Lock()


def _try_reserve_slot() -> bool:
    """占用一个并发名额；名额用完返回 False，任务留在队列里等下一轮。"""

    global _in_flight
    with _in_flight_lock:
        if _in_flight >= MAX_CONCURRENT_INTENTS:
            return False
        _in_flight += 1
        return True


def _release_slot(_future: Any = None) -> None:
    global _in_flight
    with _in_flight_lock:
        _in_flight -= 1


def poll_ai_intent_jobs() -> bool:
    """由主 worker 循环调用：有空位就领取意图任务并丢进线程池，立即返回，不等待模型。"""

    worked = False
    while _try_reserve_slot():
        with platform_session(SessionLocal) as scheduler_db:
            claimed = claim_next_job(scheduler_db, job_types=(AI_INTENT_JOB,))
            if claimed is None:
                _release_slot()
                return worked
            job_id, tenant_id = int(claimed.id), int(claimed.tenant_id)
        _executor.submit(_run_intent_job, job_id, tenant_id).add_done_callback(_release_slot)
        worked = True
    return worked


def _run_intent_job(job_id: int, tenant_id: int) -> None:
    """线程池内执行一个意图任务；任何异常都只落到该任务上，不影响其他任务。"""

    try:
        with tenant_scope(tenant_id):
            _execute_intent(job_id)
    except Exception:
        logger.exception("ai intent job crashed", extra={"job_id": job_id, "tenant_id": tenant_id})


def _execute_intent(job_id: int) -> None:
    # 阶段一：短会话，只读取并固化需要的数据。
    with tenant_session(TenantSessionLocal) as db:
        job = db.get(TaskJob, job_id)
        if job is None:
            return
        payload = parse_job_payload(job)
        try:
            prepared = ai_dispatch.prepare_intent(db, payload)
        except Exception as exc:
            fail_job(db, job, error_message=str(exc))
            logger.exception("ai intent prepare failed", extra={"job_id": job_id})
            return
        if prepared is None:
            complete_job(db, job, result={"skipped": True})
            return
        config, text, catalog, chat = prepared

    # 阶段二：不持有任何数据库会话，慢的模型调用只占用线程。
    # 有可调度功能先做意图路由；模型认为不是功能请求（或根本没有功能目录）时，
    # 若开了聊天兜底，就按人格直接聊天。路由出错、被拒绝等情况不聊天，避免误回。
    route_started = time.monotonic()
    intent = ai_dispatch.analyze_intent(config, text, catalog) if catalog else ai_dispatch.AiIntent(status="no_match", reason="没有可调度功能")
    route_seconds = time.monotonic() - route_started
    chat_reply = ""
    timing: dict[str, float] = {}
    if chat is not None and intent.status == "no_match":
        # 聊天前并行完成:按当前话题召回长期记忆,以及(问天气/价格这类实时问题时)直接预搜索。
        # 都带超时和断路保护,出问题时返回空,不影响聊天。
        started = time.monotonic()
        chat, prefetched = ai_chat.prepare_context(chat)
        timing["prepare"] = time.monotonic() - started
        try:
            started = time.monotonic()
            chat_reply = ai_chat.generate_reply(config, chat, prefetched)
            timing["reply"] = time.monotonic() - started
        except Exception as exc:
            logger.warning("ai chat reply failed", extra={"job_id": job_id})
            intent = ai_dispatch.AiIntent(status="error", reason=str(exc)[:200])
        else:
            intent = ai_dispatch.AiIntent(status="chatted", reason="按人格聊天")
        # 分步耗时(秒):下次觉得慢时直接看这条日志就知道慢在哪一步。
        logger.info(
            "ai chat timing",
            extra={"job_id": job_id, "route": round(route_seconds, 1), "prefetched": prefetched is not None, **{k: round(v, 1) for k, v in timing.items()}},
        )

    # 阶段三：短会话，落决策记录并下发结果。
    with tenant_session(TenantSessionLocal) as db:
        job = db.get(TaskJob, job_id)
        if job is None:
            return
        try:
            result = _deliver(db, payload, intent, chat_reply)
            complete_job(db, job, result=result)
        except Exception as exc:
            db.rollback()
            fail_job(db, job, error_message=str(exc))
            logger.exception("ai intent deliver failed", extra={"job_id": job_id})
            return

    # 阶段四：回复已发出，再把这条发言异步写入长期记忆；记忆服务在后台抽取事实，失败不影响已完成的聊天。
    # 群里开了"记住所有聊天"时这句话由后台批量整理,不在这里重复写入。
    if chat is not None and chat_reply and chat.memory_enabled and chat.bank_id and not chat.memory_all:
        ai_memory.retain(
            chat.bank_id,
            label=chat.own_label,
            sender_id=chat.sender_id,
            text=chat.text,
            chat_type=chat.chat_type,
            bank_name=chat.bank_name,
        )


def _execute_routed(db: Session, channel: Channel, payload: dict[str, Any], event: dict[str, Any], command_text: str) -> None:
    """识别出插件后直接在本任务里执行，不再重新入队。

    仍走原有的匹配与执行链，所以群级启停、权限、积分幂等、consume_policy 都照常生效。
    事件带 ai_rewritten 标记，保证命令不命中时不会再次进入 AI 分析。命令只在内存视图里
    替换原消息正文，库里始终只有用户原话。
    """

    from app.services.plugin_directory import build_dispatch_report

    rewritten = json.loads(json.dumps(event, ensure_ascii=False, default=str))
    rewritten.setdefault("message", {})["content"] = command_text
    rewritten.setdefault("details", {})["ai_rewritten"] = True
    with ai_dispatch.effective_message_content(db, payload.get("message_id"), command_text):
        build_dispatch_report(
            db,
            channel,
            rewritten,
            raw_event_id=payload.get("raw_event_id"),
            conversation_id=payload.get("conversation_id"),
            message_id=payload.get("message_id"),
        )


def _deliver(db: Session, payload: dict[str, Any], intent: ai_dispatch.AiIntent, chat_reply: str = "") -> dict[str, Any]:
    """记录决策；识别出插件就直接执行，缺参数就追问，聊天则发送人格回复。"""

    from app.services.plugin_directory import send_ai_reply

    channel = db.get(Channel, int(payload.get("channel_id") or 0))
    event = payload.get("normalized_event") or {}
    message = event.get("message") or {}
    conversation_id = payload.get("conversation_id")
    ai_dispatch.record_decision(
        db,
        channel_id=int(payload.get("channel_id") or 0),
        conversation_id=conversation_id,
        sender_id=message.get("sender_id"),
        text=str(message.get("content") or ""),
        intent=intent,
    )
    if channel is None:
        db.commit()
        return {"status": intent.status}
    if intent.status == "asked":
        send_ai_reply(db, channel, event, intent.question, conversation_id=conversation_id)
    elif intent.status == "chatted" and chat_reply:
        send_ai_reply(db, channel, event, chat_reply, conversation_id=conversation_id)
    elif intent.status == "routed" and intent.command_text:
        _execute_routed(db, channel, payload, event, intent.command_text)
    db.commit()
    return {"status": intent.status, "command": intent.command_text}
