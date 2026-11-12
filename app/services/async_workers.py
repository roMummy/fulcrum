from __future__ import annotations

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
from app.services.ai_intent_worker import poll_ai_intent_jobs
from app.core.text_column import dump_json_for_text_column
from app.services.analytics import evaluate_operational_alerts, record_plugin_dispatch_analytics
from app.services.channel_adapters import dispatch_outbound_message
from app.services.channel_runtime import run_due_channel_tasks
from app.services.inbound_media import download_wechat_media_attachment, mark_attachment_failed
from app.services.open_webhooks import dispatch_open_message_event, process_open_webhook_delivery_job
from app.services.group_features.dispatch import consumed_command, dispatch_group_features
from app.services.plugin_directory import build_dispatch_report, serialize_dispatch_report
from app.services.plugin_scopes import plugin_group_conversation_enabled
from app.services.plugin_runtime.scheduler import process_plugin_scheduled_action_job, schedule_due_plugin_actions
from app.services.task_queue import (
    INBOUND_MEDIA_FETCH_JOB,
    OPEN_WEBHOOK_DELIVERY_JOB,
    OUTBOUND_DISPATCH_JOB,
    AI_INTENT_JOB,
    PLUGIN_DISPATCH_JOB,
    PLUGIN_SCHEDULED_ACTION_JOB,
    claim_next_job,
    complete_job,
    fail_job,
    parse_job_payload,
    recover_running_jobs,
)


logger = get_logger(__name__)
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_last_plugin_scheduler_scan_at = 0.0
# 群聊记忆批量整理:每分钟检查一次,放在单独的后台线程里,不占用主 worker。
_last_memory_digest_at = 0.0
_memory_digest_future = None
_last_operational_alert_eval_at = 0.0
OUTBOUND_JOB_MAX_AGE = timedelta(hours=2)


def start_background_workers() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _recover_running_jobs_for_all_tenants()
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="fulcrum-worker", daemon=True)
    _worker_thread.start()
    logger.info("background worker started")


def stop_background_workers() -> None:
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=5)
    # MaiBot 每个租户绑定都有自己的 WebSocket 线程。应用退出时集中停止，避免
    # 进程重载期间保留旧连接或让远端继续向已经关闭的实例推送回复。
    from app.services.plugin_runtime.maibot_client import stop_all_maibot_connections

    stop_all_maibot_connections()
    logger.info("background worker stopped")


def _worker_loop() -> None:
    while not _stop_event.is_set():
        try:
            worked = _run_single_job()
            # AI 意图任务只在专用线程池里执行，这里只负责领取并立即返回，不等待模型。
            worked = poll_ai_intent_jobs() or worked
            scheduled = _run_due_plugin_scheduler()
            channel_result = run_due_channel_tasks()
            alerted = _run_due_operational_alerts()
            _run_memory_digest()
        except Exception:
            logger.exception("background worker loop crashed")
            worked = False
            scheduled = False
            channel_result = None
            alerted = False
        # 渠道协调器内部已经逐租户、逐任务隔离事务；这里不再持有渠道会话或识别协议类型。
        if not worked and not scheduled and not alerted and not (channel_result and channel_result.worked):
            _stop_event.wait(0.5)


def _memory_digest_all_tenants() -> None:
    from app.services import ai_memory_digest

    for tenant_id in _list_tenant_ids():
        try:
            with tenant_scope(tenant_id):
                ai_memory_digest.run_for_tenant()
        except Exception:
            logger.exception("memory digest failed", extra={"tenant_id": tenant_id})


def _run_memory_digest() -> None:
    """每分钟最多触发一次群聊记忆整理;上一次还没跑完就跳过,不会堆积。"""

    global _last_memory_digest_at, _memory_digest_future
    from concurrent.futures import ThreadPoolExecutor

    from app.services import ai_memory

    current = time.monotonic()
    if current - _last_memory_digest_at < 60 or not ai_memory.is_configured():
        return
    _last_memory_digest_at = current
    if _memory_digest_future is not None and not _memory_digest_future.done():
        return
    if not hasattr(_run_memory_digest, "_executor"):
        _run_memory_digest._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-digest")
    _memory_digest_future = _run_memory_digest._executor.submit(_memory_digest_all_tenants)


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
                    AI_INTENT_JOB,
                    PLUGIN_DISPATCH_JOB,
                    PLUGIN_SCHEDULED_ACTION_JOB,
                    OUTBOUND_DISPATCH_JOB,
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
                PLUGIN_SCHEDULED_ACTION_JOB,
                OUTBOUND_DISPATCH_JOB,
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
            elif job.job_type == PLUGIN_SCHEDULED_ACTION_JOB:
                result = process_plugin_scheduled_action_job(db, job, payload)
            elif job.job_type == OUTBOUND_DISPATCH_JOB:
                result = _process_outbound_dispatch_job(db, job, payload)
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


def _run_due_plugin_scheduler() -> bool:
    """按统一节奏逐租户扫描插件计划，不再维护插件专用内存轮询表。"""

    global _last_plugin_scheduler_scan_at
    current_monotonic = time.time()
    if current_monotonic - _last_plugin_scheduler_scan_at < 15:
        return False
    _last_plugin_scheduler_scan_at = current_monotonic

    worked = False
    for tenant_id in _list_tenant_ids():
        try:
            with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
                created_jobs = schedule_due_plugin_actions(db)
                if created_jobs:
                    db.commit()
                    worked = True
        except Exception:
            # 单个租户配置异常不能阻断其他租户计划扫描；具体任务执行仍由队列按
            # max_attempts 决定是否重试，高副作用动作默认一次失败即终止。
            logger.exception("tenant plugin schedule scan failed", extra={"tenant_id": tenant_id})
    return worked


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
    # 群功能(成员变更提醒、潜水排行、自动抢红包、自动收款等)只看群自己的开关,先于插件调度执行;
    # 命令被群功能处理后不再交给 AI 兜底。
    executed_features = dispatch_group_features(db, channel, normalized_event, conversation_id=conversation_id)
    dispatch_report = build_dispatch_report(
        db,
        channel,
        normalized_event,
        raw_event_id=raw_event_id,
        conversation_id=conversation_id,
        message_id=message_id,
        skip_ai_intent=consumed_command(executed_features),
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
        raw_event.dispatch_summary_json = dump_json_for_text_column(dispatch_payload, drop_keys=("runtime_payload", "debug"))
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
        "executed_group_features": executed_features,
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
    plugin_code = str(payload.get("plugin_code") or "").strip()
    if plugin_code:
        binding = db.scalar(select(BotPlugin).join(Plugin, Plugin.id == BotPlugin.plugin_id).where(
            BotPlugin.bot_id == channel.bot_id, Plugin.code == plugin_code,
        ))
        # 入队后仍可能解绑或关闭群权限；真正发给渠道前再核对一次。
        if (binding is not None and binding.status == "unbound") or not plugin_group_conversation_enabled(
            db, channel=channel, conversation=conversation, plugin_code=plugin_code,
        ):
            return {"skipped": True, "reason": "plugin_scope_disabled"}
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
