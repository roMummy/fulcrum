from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import desc, select

from app.core.db import SessionLocal
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Conversation, TaskJob
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.task_queue import OUTBOUND_DISPATCH_JOB, enqueue_job


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="message_push_task runtime is ready", output={})
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    operation = str(request.payload.get("operation") or request.payload.get("mode") or "").strip().lower()
    if not operation:
        operation = _infer_operation(request.payload)
    if operation == "list":
        return _list_tasks(request)
    if operation == "cancel":
        return _cancel_task(request)
    if operation == "preview":
        return _preview_task(request)
    return _create_task(request)


def _create_task(request: PluginExecutionRequest) -> PluginExecutionResult:
    conversation_id = _coerce_int(request.payload.get("conversation_id"), 0)
    channel_id = _coerce_int(request.payload.get("channel_id"), 0)
    external_id = str(request.payload.get("external_id") or request.payload.get("conversation_external_id") or "").strip()
    content = str(request.payload.get("content") or request.payload.get("message") or request.payload.get("text") or "").strip()
    if not content:
        return PluginExecutionResult(ok=False, status="error", message="定时推送缺少 content/message。")

    schedule_at = _parse_schedule_time(request.payload.get("schedule_at") or request.payload.get("available_at") or request.payload.get("run_at"))
    if schedule_at is None:
        return PluginExecutionResult(ok=False, status="error", message="schedule_at 格式错误，请使用 ISO 时间，例如 2026-03-25T14:00:00+08:00。")

    with SessionLocal() as db:
        conversation = _resolve_conversation(db, conversation_id=conversation_id, channel_id=channel_id, external_id=external_id)
        if conversation is None:
            return PluginExecutionResult(ok=False, status="error", message="未找到目标会话，请提供 conversation_id 或有效的群/会话 external_id。")
        channel = db.get(Channel, conversation.channel_id) if conversation.channel_id else None
        if channel is None:
            return PluginExecutionResult(ok=False, status="error", message="目标会话缺少关联渠道，无法创建推送任务。")

        message_type = str(request.payload.get("message_type") or "text").strip().lower() or "text"
        outbound_payload = {
            "external_id": conversation.external_id,
            "receiver_id": conversation.external_id,
            "chat_type": conversation.chat_type,
            "message_type": message_type,
            "content": content,
        }
        for key in ("xml", "base64", "image_base64", "video_base64", "voice_base64", "image_path", "file_name", "file_url"):
            value = request.payload.get(key)
            if value not in (None, ""):
                outbound_payload[key] = value

        job = enqueue_job(
            db,
            job_type=OUTBOUND_DISPATCH_JOB,
            payload={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "payload": outbound_payload,
                "plugin_code": "message_push_task",
            },
            priority=80,
            related_type="conversation",
            related_id=conversation.id,
            available_at=schedule_at,
        )
        db.commit()
        reply_text = (
            f"定时推送任务已创建。"
            f"\n任务 ID: {job.id}"
            f"\n计划时间: {schedule_at.isoformat()}"
            f"\n目标会话: {conversation.external_id}"
            f"\n消息内容: {content}"
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="定时推送任务创建成功。",
            output={
                "provider_status": "scheduled",
                "job_id": job.id,
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "schedule_at": schedule_at.isoformat(),
                "reply_text": reply_text,
                "replies": [{"type": "text", "content": reply_text}],
            },
        )


def _list_tasks(request: PluginExecutionRequest) -> PluginExecutionResult:
    conversation_id = _coerce_int(request.payload.get("conversation_id"), 0)
    with SessionLocal() as db:
        stmt = select(TaskJob).where(TaskJob.job_type == OUTBOUND_DISPATCH_JOB).order_by(desc(TaskJob.id)).limit(10)
        if conversation_id:
            stmt = stmt.where(TaskJob.related_type == "conversation", TaskJob.related_id == conversation_id)
        jobs = db.scalars(stmt).all()
        items = []
        for job in jobs:
            payload = _parse_json(job.payload_json)
            message_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
            items.append(
                {
                    "job_id": job.id,
                    "status": job.status,
                    "available_at": job.available_at.isoformat() if job.available_at else "",
                    "conversation_id": payload.get("conversation_id"),
                    "content": str(message_payload.get("content") or "").strip(),
                }
            )
    if not items:
        reply_text = "当前没有可展示的推送任务。"
    else:
        lines = ["最近的推送任务："]
        for item in items:
            lines.append(f"#{item['job_id']} {item['status']} {item['available_at']} {item['content']}")
        reply_text = "\n".join(lines)
    return PluginExecutionResult(ok=True, status="completed", message="推送任务列表生成成功。", output={"provider_status": "list", "tasks": items, "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _cancel_task(request: PluginExecutionRequest) -> PluginExecutionResult:
    job_id = _coerce_int(request.payload.get("job_id"), 0)
    if not job_id:
        return PluginExecutionResult(ok=False, status="error", message="取消任务需要 job_id。")
    with SessionLocal() as db:
        job = db.get(TaskJob, job_id)
        if job is None or job.job_type != OUTBOUND_DISPATCH_JOB:
            return PluginExecutionResult(ok=False, status="error", message="未找到可取消的推送任务。")
        if job.status in {"completed", "failed", "cancelled"}:
            return PluginExecutionResult(ok=False, status="error", message=f"任务当前状态为 {job.status}，无法取消。")
        job.status = "cancelled"
        job.finished_at = now_shanghai()
        db.commit()
    reply_text = f"推送任务 #{job_id} 已取消。"
    return PluginExecutionResult(ok=True, status="completed", message="推送任务取消成功。", output={"provider_status": "cancelled", "job_id": job_id, "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _preview_task(request: PluginExecutionRequest) -> PluginExecutionResult:
    content = str(request.payload.get("content") or request.payload.get("message") or request.payload.get("text") or "").strip()
    if not content:
        return PluginExecutionResult(ok=False, status="error", message="预览需要 content/message。")
    schedule_at = _parse_schedule_time(request.payload.get("schedule_at") or request.payload.get("available_at") or request.payload.get("run_at"))
    reply_text = f"预览推送：\n计划时间: {schedule_at.isoformat() if schedule_at else '未指定'}\n内容: {content}"
    return PluginExecutionResult(ok=True, status="completed", message="推送任务预览生成成功。", output={"provider_status": "preview", "reply_text": reply_text, "replies": [{"type": "text", "content": reply_text}]})


def _resolve_conversation(db, *, conversation_id: int, channel_id: int, external_id: str) -> Conversation | None:
    if conversation_id:
        return db.get(Conversation, conversation_id)
    if external_id:
        stmt = select(Conversation).where(Conversation.external_id == external_id).order_by(Conversation.id.desc()).limit(1)
        if channel_id:
            stmt = stmt.where(Conversation.channel_id == channel_id)
        return db.scalar(stmt)
    return None


def _parse_schedule_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return ensure_shanghai_datetime(parsed)


def _parse_json(raw: str | None) -> dict[str, object]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _infer_operation(payload: dict[str, object]) -> str:
    text = str(payload.get("command_args") or payload.get("stripped_content") or "").strip().lower()
    if not text:
        return "create"
    first = text.split()[0]
    if first in {"list", "cancel", "preview"}:
        return first
    return "create"


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
