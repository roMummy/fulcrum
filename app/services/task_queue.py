from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.settings import settings
from app.core.text_column import dump_json_for_text_column
from app.core.time import now_shanghai
from app.models.entities import TaskJob


logger = get_logger(__name__)

PLUGIN_DISPATCH_JOB = "plugin_dispatch"
PLUGIN_SCHEDULED_ACTION_JOB = "plugin_scheduled_action"
# AI 意图分析任务：模型调用慢，独立于插件调度任务，避免拖住消息主链。
AI_INTENT_JOB = "ai_intent"
OUTBOUND_DISPATCH_JOB = "outbound_dispatch"
WORKWECHAT_CLOUD_OUTBOUND_JOB = "workwechat_cloud_outbound"
WORKWECHAT_CLOUD_CONTROL_JOB = "workwechat_cloud_control"
INBOUND_MEDIA_FETCH_JOB = "inbound_media_fetch"
OPEN_WEBHOOK_DELIVERY_JOB = "open_webhook_delivery"
FINAL_STATUSES = {"completed", "failed", "cancelled"}
JOB_PAYLOAD_INLINE_LIMIT = 60000
JOB_PAYLOAD_STORAGE_VERSION = 1


def enqueue_job(
    db: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
    priority: int = 100,
    related_type: str | None = None,
    related_id: int | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 3,
    available_at: datetime | None = None,
) -> TaskJob:
    payload_json, external_payload = _serialize_job_payload(payload)
    job_kwargs = {
        "job_type": job_type,
        "status": "pending",
        "priority": priority,
        "attempts": 0,
        "max_attempts": max_attempts,
        "related_type": related_type,
        "related_id": related_id,
        "dedupe_key": str(dedupe_key or "").strip() or None,
        "payload_json": payload_json,
    }
    if job_type in {WORKWECHAT_CLOUD_OUTBOUND_JOB, WORKWECHAT_CLOUD_CONTROL_JOB}:
        # 节点级 Provider 任务的归属必须在入队时从已验证渠道绑定固化，客户端轮询
        # 只能接收这些字段，不能通过请求正文临时指定其他节点或企微账号。
        from app.models.entities import Channel
        from app.models.saas import ProviderNodeAccount

        channel_id = int(payload.get("channel_id") or 0)
        channel = db.get(Channel, channel_id) if channel_id > 0 else None
        if channel is not None and channel.provider_node_id and channel.provider_account_id:
            account = db.get(ProviderNodeAccount, int(channel.provider_account_id))
            if (
                account is not None
                and int(account.tenant_id) == int(channel.tenant_id)
                and int(account.provider_node_id) == int(channel.provider_node_id)
            ):
                job_kwargs["provider_node_id"] = int(channel.provider_node_id)
                job_kwargs["provider_account_id"] = int(channel.provider_account_id)
    if available_at is not None:
        job_kwargs["available_at"] = available_at
    job = TaskJob(**job_kwargs)
    db.add(job)
    db.flush()
    if external_payload is not None:
        storage_meta = _store_external_job_payload(job.id, external_payload)
        job.payload_json = json.dumps(storage_meta, ensure_ascii=False)
        db.flush()
    logger.info(
        "job enqueued",
        extra={"job_id": job.id, "job_type": job.job_type, "related_type": related_type or "", "related_id": related_id or 0},
    )
    return job


def claim_next_job(db: Session, *, job_types: tuple[str, ...] | None = None) -> TaskJob | None:
    current_time = now_shanghai()
    stmt = select(TaskJob).where(TaskJob.status == "pending", TaskJob.available_at <= current_time)
    if job_types:
        stmt = stmt.where(TaskJob.job_type.in_(job_types))
    stmt = stmt.order_by(TaskJob.priority.desc(), TaskJob.id.asc()).limit(1)
    job = db.scalar(stmt)
    if not job:
        return None
    job.status = "running"
    job.attempts = int(job.attempts or 0) + 1
    job.started_at = current_time
    job.locked_at = current_time
    db.commit()
    db.refresh(job)
    logger.info("job claimed", extra={"job_id": job.id, "job_type": job.job_type, "attempts": job.attempts})
    return job


def recover_running_jobs(db: Session, *, job_types: tuple[str, ...] | None = None) -> int:
    stmt = select(TaskJob).where(TaskJob.status == "running")
    if job_types:
        stmt = stmt.where(TaskJob.job_type.in_(job_types))
    jobs = db.scalars(stmt).all()
    if not jobs:
        return 0

    for job in jobs:
        job.status = "pending"
        job.started_at = None
        job.locked_at = None
        job.error_message = None

    db.commit()
    logger.warning(
        "recovered running jobs back to pending",
        extra={"job_count": len(jobs), "job_types": list(job_types or ())},
    )
    return len(jobs)


def complete_job(db: Session, job: TaskJob, *, result: dict[str, Any] | None = None) -> TaskJob:
    job.status = "completed"
    job.finished_at = now_shanghai()
    job.result_json = dump_json_for_text_column(result or {}, drop_keys=("runtime_payload", "debug"))
    job.error_message = None
    db.commit()
    db.refresh(job)
    logger.info("job completed", extra={"job_id": job.id, "job_type": job.job_type})
    return job


def fail_job(
    db: Session,
    job: TaskJob,
    *,
    error_message: str,
    retry_delay_seconds: int = 30,
    result: dict[str, Any] | None = None,
) -> TaskJob:
    can_retry = int(job.attempts or 0) < int(job.max_attempts or 0)
    job.status = "pending" if can_retry else "failed"
    job.error_message = error_message
    job.result_json = dump_json_for_text_column(result or {}, drop_keys=("runtime_payload", "debug"))
    current_time = now_shanghai()
    job.finished_at = current_time if not can_retry else None
    job.available_at = current_time + timedelta(seconds=retry_delay_seconds) if can_retry else current_time
    db.commit()
    db.refresh(job)
    logger.error(
        "job failed",
        extra={"job_id": job.id, "job_type": job.job_type, "can_retry": can_retry, "attempts": job.attempts},
    )
    return job


def fail_job_terminal(
    db: Session,
    job: TaskJob,
    *,
    error_message: str,
    result: dict[str, Any] | None = None,
) -> TaskJob:
    """把任务直接固化为失败终态，不再交给队列自动重试。

    发单类任务一旦 provider 已经领取，服务端无法证明失败回报发生在“发送前”还是“发送后”；
    这里提供显式终态失败入口，供高风险发送链路使用，避免同一订单文件被后台队列自动重发。
    """

    current_time = now_shanghai()
    job.status = "failed"
    job.error_message = error_message
    job.result_json = dump_json_for_text_column(result or {}, drop_keys=("runtime_payload", "debug"))
    job.finished_at = current_time
    job.available_at = current_time
    db.commit()
    db.refresh(job)
    logger.error(
        "job terminal failed without retry",
        extra={"job_id": job.id, "job_type": job.job_type, "attempts": job.attempts},
    )
    return job


def parse_job_payload(job: TaskJob) -> dict[str, Any]:
    raw = (job.payload_json or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict) and parsed.get("__storage__") == "file":
        return _load_external_job_payload(parsed)
    return parsed if isinstance(parsed, dict) else {}


def _serialize_job_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    serialized = json.dumps(payload, ensure_ascii=False)
    if len(serialized.encode("utf-8")) <= JOB_PAYLOAD_INLINE_LIMIT:
        return serialized, None
    return json.dumps(_build_external_payload_stub(payload), ensure_ascii=False), payload


def _build_external_payload_stub(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "__storage__": "file",
        "version": JOB_PAYLOAD_STORAGE_VERSION,
        "payload_size": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
    }


def _store_external_job_payload(job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    payload_dir = Path(settings.data_dir) / "task_job_payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False)
    digest = hashlib.sha1(serialized.encode("utf-8")).hexdigest()[:12]
    path = payload_dir / f"job_{job_id}_{digest}.json"
    path.write_text(serialized, encoding="utf-8")
    return {
        "__storage__": "file",
        "version": JOB_PAYLOAD_STORAGE_VERSION,
        "path": str(path),
        "payload_size": len(serialized.encode("utf-8")),
    }


def _load_external_job_payload(storage_meta: dict[str, Any]) -> dict[str, Any]:
    path_value = str(storage_meta.get("path") or "").strip()
    if not path_value:
        return {}
    try:
        raw = Path(path_value).read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
