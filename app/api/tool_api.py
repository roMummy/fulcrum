from __future__ import annotations

import hmac
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from sqlalchemy import select

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Conversation, Message, TaskJob
from app.models.saas import Tenant
from app.services.crud import get_channel
from app.services.outbound_files import build_public_base_url, store_outbound_upload
from app.services.task_queue import enqueue_job
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type


router = APIRouter(tags=["tool-api"])


def _parse_json(raw: str | None) -> dict[str, object]:
    import json

    normalized = (raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def enforce_tool_api_key(request: Request) -> None:
    expected = str(settings.tool_api_key or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="tool_api_key_not_configured")
    provided = str(request.headers.get("X-API-Key") or "").strip()
    if not provided:
        authorization = str(request.headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid_api_key")


def authenticate_tool_tenant(request: Request) -> int:
    """把全局工具密钥与显式租户编码组合成可信租户身份。"""

    enforce_tool_api_key(request)
    tenant_code = str(request.headers.get("X-Tenant-Code") or "").strip()
    if not tenant_code:
        raise HTTPException(status_code=400, detail="tenant_code_required")
    with platform_session(SessionLocal) as auth_db:
        tenant = auth_db.scalar(select(Tenant).where(Tenant.code == tenant_code, Tenant.status == "active"))
        if tenant is None:
            raise HTTPException(status_code=401, detail="tenant_not_found")
        return int(tenant.id)


@router.post("/api/tool/channels/{channel_id}/upload-and-send")
async def tool_upload_and_send_action(
    channel_id: int,
    request: Request,
    file: UploadFile = File(...),
    external_id: str = "",
    conversation_title: str = "",
    chat_type: str = "group",
    message_type: str = "file",
):
    tenant_id = authenticate_tool_tenant(request)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        normalized_type = str(message_type or "file").strip().lower() or "file"
        if normalized_type not in {"file", "image"}:
            raise HTTPException(status_code=400, detail="message_type must be file or image")

        normalized_external_id = str(external_id or "").strip()
        if not normalized_external_id:
            raise HTTPException(status_code=400, detail="external_id is required")

        file_bytes = await file.read()
        if not file_bytes:
            raise HTTPException(status_code=400, detail="empty file")

        stored = store_outbound_upload(
            tenant_id=channel.tenant_id,
            channel_id=channel_id,
            file_name=file.filename or "upload.bin",
            content=file_bytes,
            content_type=file.content_type,
            message_type=normalized_type,
        )
        public_base = build_public_base_url(request)
        download_url = f"{public_base}/outbound-files/{channel_id}/{stored['share_id']}/{quote(stored['file_name'])}"

        conversation = db.scalar(
            select(Conversation).where(
                Conversation.bot_id == channel.bot_id,
                Conversation.channel_id == channel.id,
                Conversation.external_id == normalized_external_id,
            )
        )
        if not conversation:
            conversation = Conversation(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                channel_type=channel.channel_type,
                external_id=normalized_external_id,
                title=str(conversation_title or "").strip() or normalized_external_id,
                chat_type=str(chat_type or "group").strip() or "group",
            )
            db.add(conversation)
            db.flush()

        outbound_payload = {
            "external_id": normalized_external_id,
            "message_type": normalized_type,
            "file_name": stored["file_name"],
            "chat_type": str(chat_type or "group").strip() or "group",
            "title": str(conversation_title or "").strip() or conversation.title or normalized_external_id,
        }
        if normalized_type == "image":
            outbound_payload["image_url"] = download_url
        else:
            outbound_payload["file_url"] = download_url

        job = enqueue_job(
            db,
            job_type=resolve_outbound_job_type(channel),
            payload={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "payload": outbound_payload,
            },
            priority=90,
            related_type="conversation",
            related_id=conversation.id,
        )
        notify_cloud_job_enqueued(channel, job)
        db.commit()
        return {
            "ok": True,
            "queued": True,
            "job_id": job.id,
            "channel_id": channel.id,
            "conversation_id": conversation.id,
            "external_id": normalized_external_id,
            "message_type": normalized_type,
            "file_name": stored["file_name"],
            "file_size": stored["file_size"],
            "download_url": download_url,
            "payload": outbound_payload,
        }


@router.get("/api/tool/channels/{channel_id}/messages")
async def tool_channel_messages_api(
    channel_id: int,
    request: Request,
    external_id: str = "",
    after_id: int = 0,
    limit: int = 50,
):
    tenant_id = authenticate_tool_tenant(request)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")

        normalized_limit = max(1, min(int(limit or 50), 200))
        stmt = (
            select(Message, Conversation)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.channel_id == channel_id)
        )
        normalized_external_id = str(external_id or "").strip()
        if normalized_external_id:
            stmt = stmt.where(Conversation.external_id == normalized_external_id)
        if int(after_id or 0) > 0:
            stmt = stmt.where(Message.id > int(after_id))
        rows = db.execute(stmt.order_by(Message.id.asc()).limit(normalized_limit)).all()
        messages = []
        last_id = after_id if after_id > 0 else 0
        for message, conversation in rows:
            last_id = max(int(message.id or 0), int(last_id or 0))
            messages.append(
                {
                    "id": message.id,
                    "conversation_id": conversation.id,
                    "conversation_external_id": conversation.external_id,
                    "conversation_title": conversation.title,
                    "chat_type": conversation.chat_type,
                    "direction": message.direction,
                    "sender_id": message.sender_id,
                    "sender_name": message.sender_name,
                    "content": message.content,
                    "message_type": message.message_type,
                    "external_message_id": message.external_message_id,
                    "status": message.status,
                    "external_status": message.external_status,
                    "created_at": message.created_at.isoformat() if getattr(message, "created_at", None) else None,
                }
            )
        return {
            "ok": True,
            "channel_id": channel_id,
            "external_id": normalized_external_id or None,
            "after_id": after_id,
            "last_id": last_id,
            "count": len(messages),
            "messages": messages,
        }


@router.get("/api/tool/jobs/{job_id}")
async def tool_job_status_api(job_id: int, request: Request):
    tenant_id = authenticate_tool_tenant(request)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        job = db.get(TaskJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "ok": True,
            "job": {
                "id": job.id,
                "job_type": job.job_type,
                "status": job.status,
                "priority": job.priority,
                "attempts": job.attempts,
                "max_attempts": job.max_attempts,
                "available_at": job.available_at.isoformat() if job.available_at else None,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                "related_type": job.related_type,
                "related_id": job.related_id,
                "payload": _parse_json(job.payload_json),
                "result": _parse_json(job.result_json) if job.result_json else None,
                "error_message": job.error_message,
            },
        }
