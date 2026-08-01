from __future__ import annotations

import json
import threading
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.logging import get_logger
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Channel, Conversation, ErrorLog, Message, MessageAttachment, Plugin, RawWebhookEvent, TaskJob
from app.models.saas import Tenant
from app.services.analytics import evaluate_operational_alerts, record_plugin_dispatch_analytics
from app.services.channel_adapters import dispatch_outbound_message
from app.services.inbound_media import download_wechat_media_attachment, mark_attachment_failed
from app.services.open_webhooks import dispatch_open_message_event, process_open_webhook_delivery_job
from app.services.plugin_directory import build_dispatch_report, serialize_dispatch_report
from app.services.task_queue import (
    INBOUND_MEDIA_FETCH_JOB,
    OPEN_WEBHOOK_DELIVERY_JOB,
    OUTBOUND_DISPATCH_JOB,
    PLUGIN_DISPATCH_JOB,
    WEATHER_PUSH_JOB,
    claim_next_job,
    complete_job,
    fail_job,
    parse_job_payload,
    recover_running_jobs,
)
from app.services.telegram_polling import poll_telegram_channel
from app.services.wechat_ipad import (
    auto_recover_wechat_ipad_login,
    heartbeat_wechat_ipad_channel,
    pull_wechat_ipad_messages,
)


logger = get_logger(__name__)
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_last_telegram_poll_at = 0.0
_last_wechat_ipad_message_sync_at: dict[int, float] = {}
_last_douyu_poll_at: dict[int, float] = {}
_last_daily_news_poll_at: dict[int, float] = {}
_last_operational_alert_eval_at = 0.0
WECHAT_IPAD_MESSAGE_SYNC_INTERVAL_SECONDS = 2.0
OUTBOUND_JOB_MAX_AGE = timedelta(hours=2)


def start_background_workers() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _recover_running_jobs_for_all_tenants()
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="multi-bot-admin-worker", daemon=True)
    _worker_thread.start()
    logger.info("background worker started")


def stop_background_workers() -> None:
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=5)
    logger.info("background worker stopped")


def _worker_loop() -> None:
    while not _stop_event.is_set():
        try:
            worked = _run_single_job()
            alerted = _run_due_operational_alerts()
        except Exception:
            logger.exception("background worker loop crashed")
            worked = False
            alerted = False
        # SaaS 首期只开放企业微信，云端 Provider 通过独立长轮询接口领取任务。
        # 旧 Telegram、微信 iPad 和插件定时轮询函数暂时保留供代码迁移参考，
        # 但不再从生产 Worker 主循环触发，避免平台会话跨租户扫描旧渠道。
        if not worked and not alerted:
            _stop_event.wait(0.5)


def _list_tenant_ids() -> list[int]:
    """使用平台会话只读取租户主键，业务处理必须另开租户会话。"""

    with platform_session(SessionLocal) as db:
        return [int(tenant_id) for tenant_id in db.scalars(select(Tenant.id).order_by(Tenant.id.asc())).all()]


def _recover_running_jobs_for_all_tenants() -> int:
    """启动时按租户恢复中断任务，禁止平台会话直接修改租户任务。"""

    recovered_count = 0
    for tenant_id in _list_tenant_ids():
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            recovered_count += recover_running_jobs(
                db,
                job_types=(
                    PLUGIN_DISPATCH_JOB,
                    OUTBOUND_DISPATCH_JOB,
                    WEATHER_PUSH_JOB,
                    INBOUND_MEDIA_FETCH_JOB,
                    OPEN_WEBHOOK_DELIVERY_JOB,
                ),
            )
    return recovered_count


def _run_single_job() -> bool:
    # 调度阶段只在平台会话领取任务并复制最小身份，不能把平台会话中的 ORM 对象
    # 直接交给执行阶段使用。tenant_id 来自任务表本身，不读取 payload 同名字段。
    with platform_session(SessionLocal) as scheduler_db:
        claimed_job = claim_next_job(
            scheduler_db,
            job_types=(
                PLUGIN_DISPATCH_JOB,
                OUTBOUND_DISPATCH_JOB,
                WEATHER_PUSH_JOB,
                INBOUND_MEDIA_FETCH_JOB,
                OPEN_WEBHOOK_DELIVERY_JOB,
            ),
        )
        if not claimed_job:
            return False
        job_id = int(claimed_job.id)
        tenant_id = int(claimed_job.tenant_id)

    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        job = db.get(TaskJob, job_id)
        if not job:
            # 任务在领取后若无法从绑定租户重新加载，说明租户归属或数据状态异常。
            # 不尝试切回平台会话执行，避免在隔离失效时调用任何外部 Provider。
            logger.error("claimed job missing in tenant scope", extra={"job_id": job_id, "tenant_id": tenant_id})
            return True
        try:
            payload = parse_job_payload(job)
            if job.job_type == PLUGIN_DISPATCH_JOB:
                result = _process_plugin_dispatch_job(db, payload)
            elif job.job_type == OUTBOUND_DISPATCH_JOB:
                result = _process_outbound_dispatch_job(db, job, payload)
            elif job.job_type == WEATHER_PUSH_JOB:
                result = _process_weather_push_job(payload)
            elif job.job_type == INBOUND_MEDIA_FETCH_JOB:
                result = _process_inbound_media_fetch_job(db, payload)
            elif job.job_type == OPEN_WEBHOOK_DELIVERY_JOB:
                result = process_open_webhook_delivery_job(db, payload)
            else:
                raise ValueError(f"unsupported job type: {job.job_type}")
            complete_job(db, job, result=result)
            return True
        except Exception as exc:
            fail_job(db, job, error_message=str(exc))
            logger.exception(
                "job processing failed",
                extra={"job_id": job.id, "job_type": job.job_type, "tenant_id": tenant_id},
            )
            return True


def _run_due_channel_pollers() -> bool:
    global _last_telegram_poll_at
    now = time.time()
    if now - _last_telegram_poll_at < 3:
        return False
    _last_telegram_poll_at = now

    db = SessionLocal()
    try:
        channels = (
            db.query(Channel)
            .filter(Channel.channel_type == "telegram", Channel.is_enabled.is_(True))
            .order_by(Channel.id.asc())
            .all()
        )
        worked = False
        for channel in channels:
            result = poll_telegram_channel(db, channel)
            worked = worked or bool(result.get("processed")) or bool(result.get("error"))
        return worked
    finally:
        db.close()


def _run_due_douyu_pollers() -> bool:
    from plugins.douyu.plugin import poll_douyu_binding

    now = time.time()
    db = SessionLocal()
    try:
        bindings = db.scalars(
            select(BotPlugin)
            .join(Plugin, Plugin.id == BotPlugin.plugin_id)
            .where(Plugin.code == "douyu", BotPlugin.status == "enabled")
            .order_by(BotPlugin.id.asc())
        ).all()
        worked = False
        for binding in bindings:
            interval = 60
            try:
                raw_config = json.loads((binding.config_json or "").strip() or "{}")
                if isinstance(raw_config, dict):
                    interval = max(30, int(raw_config.get("poll_interval_seconds") or 60))
            except Exception:
                interval = 60
            elapsed = now - _last_douyu_poll_at.get(binding.id, 0.0)
            if elapsed < interval:
                continue
            _last_douyu_poll_at[binding.id] = now
            result = poll_douyu_binding(binding.id)
            worked = worked or bool(result.get("event_count")) or not bool(result.get("skipped"))
        return worked
    finally:
        db.close()


def _run_due_daily_news_pollers() -> bool:
    from plugins.daily_news.plugin import run_daily_news_push

    now = time.time()
    db = SessionLocal()
    try:
        bindings = db.scalars(
            select(BotPlugin)
            .join(Plugin, Plugin.id == BotPlugin.plugin_id)
            .where(Plugin.code == "daily_news", BotPlugin.status == "enabled")
            .order_by(BotPlugin.id.asc())
        ).all()
        worked = False
        for binding in bindings:
            elapsed = now - _last_daily_news_poll_at.get(binding.id, 0.0)
            if elapsed < 30:
                continue
            _last_daily_news_poll_at[binding.id] = now
            result = run_daily_news_push(binding.id)
            worked = worked or bool(result.get("outbound_job_ids")) or not bool(result.get("skipped"))
        return worked
    finally:
        db.close()


def _run_due_wechat_ipad_heartbeats() -> bool:
    db = SessionLocal()
    try:
        channels = (
            db.query(Channel)
            .filter(
                Channel.channel_type == "wechat_ipad",
                Channel.is_enabled.is_(True),
                Channel.login_state == "online",
            )
            .order_by(Channel.id.asc())
            .all()
        )
        worked = False
        for channel in channels:
            result = heartbeat_wechat_ipad_channel(db, channel)
            worked = worked or (not bool(result.get("skipped")))
        return worked
    finally:
        db.close()


def _run_due_wechat_ipad_message_sync() -> bool:
    now = time.time()
    db = SessionLocal()
    try:
        channels = (
            db.query(Channel)
            .filter(
                Channel.channel_type == "wechat_ipad",
                Channel.is_enabled.is_(True),
                Channel.login_state == "online",
            )
            .order_by(Channel.id.asc())
            .all()
        )
        if not channels:
            return False
        worked = False
        for channel in channels:
            last_sync_at = _last_wechat_ipad_message_sync_at.get(channel.id, 0.0)
            elapsed = now - last_sync_at
            if elapsed < WECHAT_IPAD_MESSAGE_SYNC_INTERVAL_SECONDS:
                logger.debug(
                    "wechat ipad message sync skipped",
                    extra={
                        "channel_id": channel.id,
                        "reason": "interval_not_elapsed",
                        "elapsed_seconds": round(elapsed, 3),
                        "interval_seconds": WECHAT_IPAD_MESSAGE_SYNC_INTERVAL_SECONDS,
                    },
                )
                continue
            _last_wechat_ipad_message_sync_at[channel.id] = now
            logger.debug(
                "wechat ipad message sync scheduled",
                extra={
                    "channel_id": channel.id,
                    "interval_seconds": WECHAT_IPAD_MESSAGE_SYNC_INTERVAL_SECONDS,
                },
            )
            result = pull_wechat_ipad_messages(db, channel)
            logger.info(
                "wechat ipad message sync completed",
                extra={
                    "channel_id": channel.id,
                    "message_count": result.get("last_message_sync_count"),
                    "ingested": result.get("last_message_sync_ingested"),
                    "deduplicated": result.get("last_message_sync_deduplicated"),
                    "error": result.get("last_error"),
                },
            )
            worked = worked or (not bool(result.get("skipped")))
        return worked
    finally:
        db.close()


def _run_due_wechat_ipad_auto_recover() -> bool:
    db = SessionLocal()
    try:
        channels = (
            db.query(Channel)
            .filter(
                Channel.channel_type == "wechat_ipad",
                Channel.is_enabled.is_(True),
                Channel.login_state.in_(("offline", "reconnect_failed", "reconnect_blocked", "idle", "error")),
            )
            .order_by(Channel.id.asc())
            .all()
        )
        worked = False
        for channel in channels:
            result = auto_recover_wechat_ipad_login(db, channel)
            worked = worked or (not bool(result.get("skipped")))
        return worked
    finally:
        db.close()


def _process_plugin_dispatch_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    channel = db.get(Channel, int(payload.get("channel_id") or 0))
    if not channel:
        raise ValueError("channel_not_found")
    normalized_event = payload.get("normalized_event")
    if not isinstance(normalized_event, dict):
        raise ValueError("normalized_event_missing")

    raw_event_id = _coerce_int(payload.get("raw_event_id"))
    conversation_id = _coerce_int(payload.get("conversation_id"))
    message_id = _coerce_int(payload.get("message_id"))
    dispatch_report = build_dispatch_report(
        db,
        channel,
        normalized_event,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    dispatch_payload = serialize_dispatch_report(dispatch_report)
    _record_plugin_dispatch_errors(
        db,
        channel=channel,
        dispatch_report=dispatch_report,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    record_plugin_dispatch_analytics(
        db,
        channel=channel,
        dispatch_report=dispatch_report,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    raw_event = db.get(RawWebhookEvent, raw_event_id) if raw_event_id else None
    if raw_event:
        raw_event.dispatch_summary_json = json.dumps(dispatch_payload, ensure_ascii=False)
        raw_event.status = "dispatched"
    db.commit()
    logger.info(
        "plugin dispatch job processed",
        extra={
            "raw_event_id": raw_event_id or 0,
            "channel_id": channel.id,
            "executed_plugins": list(dispatch_report.executed_plugin_codes),
        },
    )
    return {
        "dispatch": dispatch_payload,
        "raw_event_id": raw_event_id,
        "executed_plugins": list(dispatch_report.executed_plugin_codes),
    }


def _record_plugin_dispatch_errors(
    db: Session,
    *,
    channel: Channel,
    dispatch_report: Any,
    raw_event_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
) -> None:
    for execution in getattr(dispatch_report, "executions", ()) or ():
        status = str(getattr(execution, "status", "") or "").strip().lower()
        if status not in {"error", "timeout"}:
            continue
        plugin_code = str(getattr(getattr(execution, "candidate", None), "plugin_code", "") or "").strip() or "unknown"
        detail = str(getattr(execution, "message", "") or "").strip() or status
        db.add(
            ErrorLog(
                source=f"plugin:{plugin_code}",
                level="ERROR",
                message=(
                    f"插件执行异常 | plugin={plugin_code} | status={status} | "
                    f"channel_id={channel.id} | conversation_id={conversation_id or 0} | "
                    f"message_id={message_id or 0} | raw_event_id={raw_event_id or 0} | detail={detail}"
                ),
            )
        )


def _process_outbound_dispatch_job(db: Session, job: TaskJob, payload: dict[str, Any]) -> dict[str, Any]:
    channel = db.get(Channel, int(payload.get("channel_id") or 0))
    if not channel:
        raise ValueError("channel_not_found")
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    if not conversation:
        raise ValueError("conversation_not_found")
    if int(conversation.channel_id or 0) != int(channel.id):
        # 两个资源即使都属于当前租户，也不能把甲渠道任务重定向到乙渠道会话。
        raise ValueError("conversation_channel_mismatch")
    outbound_payload = payload.get("payload")
    if not isinstance(outbound_payload, dict):
        raise ValueError("payload_missing")
    if _is_expired_outbound_job(job):
        age_seconds = _outbound_job_age_seconds(job)
        logger.warning(
            "skipping expired outbound dispatch job",
            extra={
                "job_id": job.id,
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "age_seconds": age_seconds,
            },
        )
        return {
            "skipped": True,
            "reason": "expired",
            "max_age_seconds": int(OUTBOUND_JOB_MAX_AGE.total_seconds()),
            "age_seconds": age_seconds,
        }
    message, adapter_result = dispatch_outbound_message(db, channel, conversation, outbound_payload)
    operation_id = int(payload.get("customer_operation_request_id") or 0)
    if operation_id:
        from app.services.customer_operations import mark_customer_operation_dispatched

        mark_customer_operation_dispatched(db, operation_id=operation_id, message=message, adapter_result=adapter_result)
        db.commit()
    if channel.channel_type == "wechat_ipad":
        from app.services.wechat_ipad.service import record_wechat_ipad_send_event

        record_wechat_ipad_send_event(db, channel, adapter_result=adapter_result, payload=outbound_payload)
    logger.info(
        "outbound dispatch job processed",
        extra={"channel_id": channel.id, "conversation_id": conversation.id, "message_id": message.id, "ok": bool(adapter_result.get("ok"))},
    )
    return {"message_id": message.id, "adapter_result": adapter_result}


def _is_expired_outbound_job(job: TaskJob) -> bool:
    created_at = getattr(job, "created_at", None)
    if created_at is None:
        return False
    return now_shanghai() - ensure_shanghai_datetime(created_at) > OUTBOUND_JOB_MAX_AGE


def _outbound_job_age_seconds(job: TaskJob) -> int:
    created_at = getattr(job, "created_at", None)
    if created_at is None:
        return 0
    return max(0, int((now_shanghai() - ensure_shanghai_datetime(created_at)).total_seconds()))


def _process_weather_push_job(payload: dict[str, Any]) -> dict[str, Any]:
    from plugins.weather.plugin import process_weather_push_job

    return process_weather_push_job(payload)


def _process_inbound_media_fetch_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    attachment = db.get(MessageAttachment, int(payload.get("attachment_id") or 0))
    if not attachment:
        raise ValueError("attachment_not_found")
    channel = db.get(Channel, int(payload.get("channel_id") or attachment.channel_id or 0))
    if not channel:
        raise ValueError("channel_not_found")
    if int(attachment.channel_id or 0) != int(channel.id):
        raise ValueError("attachment_channel_mismatch")
    try:
        result = download_wechat_media_attachment(db, attachment=attachment, channel=channel)
        if result.get("queued_cloud_compensation"):
            # 云端模式只是把下载动作交给 Windows provider，文件尚未落到主服务；
            # file.received 必须等控制任务完成并写入 storage_path 后再补发。
            db.commit()
            logger.info(
                "inbound media fetch converted to cloud compensation job",
                extra={"attachment_id": attachment.id, "channel_id": channel.id, "cloud_job_id": result.get("cloud_job_id")},
            )
            return result
        message = db.get(Message, int(attachment.message_id or 0)) if attachment.message_id else None
        conversation = db.get(Conversation, int(attachment.conversation_id or 0)) if attachment.conversation_id else None
        if message is not None and conversation is not None:
            # 异步下载完成后再触发 file.received，确保业务系统收到事件时附件已经可读取。
            dispatch_open_message_event(
                db,
                event_type="file.received",
                channel=channel,
                conversation=conversation,
                message=message,
                attachments=[attachment],
            )
        db.commit()
        logger.info(
            "inbound media fetched",
            extra={"attachment_id": attachment.id, "channel_id": channel.id, "status": attachment.status},
        )
        return result
    except Exception as exc:
        mark_attachment_failed(db, attachment=attachment, error_message=str(exc))
        db.commit()
        raise


def _run_due_operational_alerts() -> bool:
    global _last_operational_alert_eval_at
    now = time.time()
    if now - _last_operational_alert_eval_at < 60:
        return False
    _last_operational_alert_eval_at = now
    worked = False
    for tenant_id in _list_tenant_ids():
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            result = evaluate_operational_alerts(db)
            logger.debug("operational alerts evaluated", extra={"tenant_id": tenant_id, **result})
            worked = worked or bool(result.get("active") or result.get("resolved"))
    return worked


def _coerce_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None
