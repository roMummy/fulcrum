from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel, Conversation
from app.services.crud import get_channel
from app.services.ops import record_audit
from app.services.outbound_files import OUTBOUND_FILE_ROOT, build_public_base_url, resolve_tenant_share_file, store_outbound_upload
from app.services.saas_auth import has_tenant_role
from app.services.task_queue import enqueue_job
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type


router = APIRouter(tags=["channel-outbound"])


def enforce_ops_role(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Permission denied")
    return user


@router.post("/channels/{channel_id}/outbound/upload")
async def outbound_upload_action(
    channel_id: int,
    request: Request,
    file: UploadFile = File(...),
    message_type: str = Form("file"),
):
    enforce_ops_role(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
    finally:
        db.close()

    normalized_type = str(message_type or "file").strip().lower() or "file"
    if normalized_type not in {"file", "image"}:
        raise HTTPException(status_code=400, detail="message_type must be file or image")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")
    stored = store_outbound_upload(
        tenant_id=channel.tenant_id,
        channel_id=channel_id,
        file_name=file.filename or "upload.bin",
        content=content,
        content_type=file.content_type,
        message_type=normalized_type,
    )

    public_base = build_public_base_url(request)
    download_url = f"{public_base}/outbound-files/{channel_id}/{stored['share_id']}/{quote(stored['file_name'])}"
    return {
        "ok": True,
        "channel_id": channel_id,
        "message_type": normalized_type,
        "file_name": stored["file_name"],
        "file_size": stored["file_size"],
        "content_type": stored["content_type"],
        "share_id": stored["share_id"],
        "download_url": download_url,
        "payload": {
            "message_type": normalized_type,
            "file_name": stored["file_name"],
            "image_url": download_url if normalized_type == "image" else None,
            "file_url": download_url if normalized_type == "file" else None,
        },
    }


@router.get("/outbound-files/{channel_id}/{share_id}/{file_name}")
async def outbound_file_download(channel_id: int, share_id: str, file_name: str):
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="file not found")
        tenant_id = int(channel.tenant_id)
    target_path = resolve_tenant_share_file(
        root=OUTBOUND_FILE_ROOT,
        tenant_id=tenant_id,
        namespace="outbound_files",
        channel_id=channel_id,
        share_id=share_id,
        file_name=file_name,
    )
    if target_path is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path=target_path, filename=target_path.name)


@router.post("/channels/{channel_id}/outbound/upload-and-send")
async def outbound_upload_and_send_action(
    channel_id: int,
    request: Request,
    file: UploadFile = File(...),
    external_id: str = Form(...),
    conversation_title: str = Form(""),
    chat_type: str = Form("group"),
    message_type: str = Form("file"),
):
    enforce_ops_role(request)
    db = open_tenant_session(TenantSessionLocal)
    try:
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
        record_audit(
            db,
            "message.upload-and-send-queued",
            "task_job",
            job.id,
            f"channel={channel.id}, conversation={conversation.id}, file={stored['file_name']}",
        )
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
            "adapter": {"adapter_type": channel.channel_type, "transport_status": "queued", "status": "pending"},
        }
    finally:
        db.close()
