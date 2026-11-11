from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Channel, Chatroom, Conversation, OpenApiApp, OpenApiAppChannel, TaskJob
from app.services.channel_catalog import list_supported_channel_types
from app.services.open_api_auth import OpenApiAuthError, OpenApiAuthResult, authenticate_open_api_request
from app.services.open_api_guard import OpenApiGuardError, OpenApiGuardResult, guard_open_api_request
from app.services.open_api_quotas import finalize_open_api_usage
from app.services.outbound_files import build_public_base_url, store_outbound_upload
from app.services.task_queue import enqueue_job, parse_job_payload
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type


router = APIRouter(tags=["open-api"])
SUPPORTED_CHANNEL_TYPES = list_supported_channel_types()


@router.post("/api/open/files")
async def open_send_file_api(request: Request):
    """开放接口：发送文件。

    业务系统只需要提供 request_id、目标会话和文件信息；
    Fulcrum 负责落地任务，并按渠道模式交给本地 Provider 或云端 worker 执行。
    """

    raw_body = await request.body()
    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, raw_body)
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        payload = _parse_json_body(raw_body)
        channel_id = int(payload.get("channel_id") or 0) or None
        request_id = str(payload.get("request_id") or "").strip()
        guard = _guard_request(
            db,
            app=app,
            request_id=request_id,
            channel_id=channel_id,
        )
        result = enqueue_open_file_request(db, app=app, payload=payload, public_base_url=build_public_base_url(request))
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.post("/api/open/messages")
async def open_send_message_api(request: Request):
    """开放接口：发送普通消息。

    文件类消息走 `/api/open/files`；这里承载文本、图片 URL、链接和小程序等轻量消息，
    便于外部业务系统把 Fulcrum 当作统一消息网关调用。
    """

    raw_body = await request.body()
    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, raw_body)
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        payload = _parse_json_body(raw_body)
        channel_id = int(payload.get("channel_id") or 0) or None
        request_id = str(payload.get("request_id") or "").strip()
        guard = _guard_request(
            db,
            app=app,
            request_id=request_id,
            usage={"api_requests": 1, "messages": 1},
            channel_id=channel_id,
        )
        result = enqueue_open_message_request(db, app=app, payload=payload)
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/jobs/{request_id}")
async def open_job_status_api(request_id: str, request: Request):
    """开放接口：按业务 request_id 查询任务状态。"""

    raw_body = b""
    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, raw_body)
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request_id,
        )
        job = find_open_api_job(db, app=app, request_id=request_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return {"ok": True, "request_id": request_id, "job": serialize_open_api_job(job)}


@router.get("/api/open/conversations")
async def open_conversations_api(request: Request, chat_type: str = "", q: str = "", limit: int = 200):
    """开放接口：列出当前应用默认渠道下的会话候选项。

    业务系统做群绑定时不应该再靠人工从 Fulcrum 后台复制多个 ID；
    这里把 Conversation 和 Chatroom 快照合并输出，收发单系统可以直接选择。
    """

    raw_body = b""
    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, raw_body)
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
        )
        items = list_open_conversation_options(db, app=app, chat_type=chat_type, q=q, limit=limit)
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return {"ok": True, "items": items}


@router.get("/api/open/chatrooms")
async def open_chatrooms_api(request: Request, q: str = "", limit: int = 500):
    """开放接口：列出当前应用授权渠道下的群。

    业务系统只需要理解“企微号/渠道 + 群”，不需要理解 Fulcrum 内部 Conversation；
    发送时仍使用群 external_id 作为 conversation_external_id。
    """

    raw_body = b""
    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, raw_body)
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
        )
        items = list_open_chatroom_options(db, app=app, q=q, limit=limit)
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return {"ok": True, "items": items}


def enqueue_open_message_request(db: Session, *, app: OpenApiApp, payload: dict[str, Any]) -> dict[str, Any]:
    """创建开放接口普通消息发送任务。

    与文件发送共用 request_id 幂等机制，业务系统网络重试不会造成重复发消息。
    """

    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id_required")
    existing_job = find_open_api_job(db, app=app, request_id=request_id)
    if existing_job is not None:
        return {"ok": True, "idempotent": True, "request_id": request_id, "job": serialize_open_api_job(existing_job)}

    channel = _resolve_open_api_channel_from_payload(db, app=app, payload=payload)
    conversation = _ensure_open_conversation_from_payload(db, channel=channel, payload=payload)
    message_type = str(payload.get("message_type") or payload.get("type") or "text").strip().lower() or "text"
    if message_type == "file":
        raise HTTPException(status_code=400, detail="file_message_use_files_api")
    if message_type not in {"text", "image", "voice", "video", "emoji", "link", "card", "mini_program", "app"}:
        raise HTTPException(status_code=400, detail="unsupported_message_type")
    outbound_payload = _build_open_message_payload(conversation=conversation, payload=payload, message_type=message_type)
    job = enqueue_job(
        db,
        job_type=resolve_outbound_job_type(channel),
        payload={
            "channel_id": channel.id,
            "conversation_id": conversation.id,
            "payload": outbound_payload,
            "open_api": {
                "app_key": app.app_key,
                "request_id": request_id,
            },
        },
        priority=90,
        related_type="open_api_request",
        related_id=conversation.id,
    )
    notify_cloud_job_enqueued(channel, job)
    return {
        "ok": True,
        "queued": True,
        "idempotent": False,
        "request_id": request_id,
        "job": serialize_open_api_job(job),
        "channel_id": channel.id,
        "conversation_id": conversation.id,
    }


def list_open_conversation_options(db: Session, *, app: OpenApiApp, chat_type: str = "", q: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """列出开放应用可绑定的 Fulcrum 会话。

    返回应用授权渠道范围内的数据；Conversation 是消息会话主表，
    Chatroom 是群登记表，两边按 external_id 合并。
    """

    normalized_chat_type = str(chat_type or "").strip().lower()
    keyword = str(q or "").strip().lower()
    max_items = min(max(1, int(limit or 200)), 500)
    items: list[dict[str, Any]] = []
    for channel in _load_authorized_channels(db, app):
        chatrooms = db.scalars(select(Chatroom).where(Chatroom.channel_id == channel.id)).all()
        chatrooms_by_external_id = {str(item.external_id or "").strip(): item for item in chatrooms if str(item.external_id or "").strip()}
        seen_external_ids: set[str] = set()
        stmt = select(Conversation).where(Conversation.channel_id == channel.id).order_by(Conversation.id.desc())
        for conversation in db.scalars(stmt).all():
            if normalized_chat_type and str(conversation.chat_type or "").strip().lower() != normalized_chat_type:
                continue
            chatroom = chatrooms_by_external_id.get(str(conversation.external_id or "").strip())
            item = _serialize_open_conversation_option(channel=channel, conversation=conversation, chatroom=chatroom)
            if not _conversation_option_matches(item, keyword):
                continue
            items.append(item)
            seen_external_ids.add(str(conversation.external_id or "").strip())
            if len(items) >= max_items:
                return items
        if normalized_chat_type in {"", "group"}:
            for chatroom in sorted(chatrooms, key=lambda row: int(row.id or 0), reverse=True):
                external_id = str(chatroom.external_id or "").strip()
                if not external_id or external_id in seen_external_ids:
                    continue
                item = _serialize_open_conversation_option(channel=channel, conversation=None, chatroom=chatroom)
                if not _conversation_option_matches(item, keyword):
                    continue
                items.append(item)
                if len(items) >= max_items:
                    return items
    return items


def list_open_chatroom_options(db: Session, *, app: OpenApiApp, q: str = "", limit: int = 500) -> list[dict[str, Any]]:
    """列出开放应用已授权渠道下的群。

    这是业务系统优先使用的接口：业务上只暴露“企微号/渠道 + 群”，
    不要求外部系统理解 Fulcrum 内部 Conversation。
    """

    keyword = str(q or "").strip().lower()
    max_items = min(max(1, int(limit or 500)), 1000)
    items: list[dict[str, Any]] = []
    for channel in _load_authorized_channels(db, app):
        stmt = select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.id.desc())
        for chatroom in db.scalars(stmt).all():
            item = _serialize_open_chatroom_option(channel=channel, chatroom=chatroom)
            if not _chatroom_option_matches(item, keyword):
                continue
            items.append(item)
            if len(items) >= max_items:
                return items
    return items


def enqueue_open_file_request(db: Session, *, app: OpenApiApp, payload: dict[str, Any], public_base_url: str) -> dict[str, Any]:
    """创建开放接口文件发送任务。

    request_id 是业务幂等键。重复提交相同 request_id 时直接返回原任务，
    避免业务系统重试造成供应商群重复收到同一个发单文件。
    """

    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="request_id_required")
    existing_job = find_open_api_job(db, app=app, request_id=request_id)
    if existing_job is not None:
        return {"ok": True, "idempotent": True, "request_id": request_id, "job": serialize_open_api_job(existing_job)}

    channel = _resolve_open_api_channel_from_payload(db, app=app, payload=payload)
    conversation = _ensure_open_conversation_from_payload(db, channel=channel, payload=payload)
    file_name = str(payload.get("file_name") or "").strip() or "upload.bin"
    content = str(payload.get("content") or "").strip()

    file_url, stored_meta = _resolve_open_file_url(
        tenant_id=int(channel.tenant_id),
        channel_id=int(channel.id),
        payload=payload,
        file_name=file_name,
        public_base_url=public_base_url,
    )
    outbound_payload = {
        "external_id": conversation.external_id,
        "receiver_id": conversation.external_id,
        "chat_type": conversation.chat_type,
        "message_type": "file",
        "file_name": file_name,
        "file_url": file_url,
        "content": content,
        "title": conversation.title or conversation.external_id,
    }
    job = enqueue_job(
        db,
        job_type=resolve_outbound_job_type(channel),
        payload={
            "channel_id": channel.id,
            "conversation_id": conversation.id,
            "payload": outbound_payload,
            "open_api": {
                "app_key": app.app_key,
                "request_id": request_id,
                "stored_file": stored_meta,
            },
        },
        priority=90,
        related_type="open_api_request",
        related_id=conversation.id,
    )
    notify_cloud_job_enqueued(channel, job)
    return {
        "ok": True,
        "queued": True,
        "idempotent": False,
        "request_id": request_id,
        "job": serialize_open_api_job(job),
        "channel_id": channel.id,
        "conversation_id": conversation.id,
        "file_url": file_url,
    }


def find_open_api_job(db: Session, *, app: OpenApiApp, request_id: str) -> TaskJob | None:
    """按 app_key 和 request_id 查找开放接口任务。"""

    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        return None
    candidates = db.scalars(
        select(TaskJob)
        .where(TaskJob.related_type == "open_api_request")
        .order_by(TaskJob.id.desc())
        .limit(500)
    ).all()
    for job in candidates:
        payload = parse_job_payload(job)
        open_api = payload.get("open_api") if isinstance(payload.get("open_api"), dict) else {}
        if open_api.get("app_key") == app.app_key and open_api.get("request_id") == normalized_request_id:
            return job
    return None


def serialize_open_api_job(job: TaskJob) -> dict[str, Any]:
    """序列化开放接口任务状态。"""

    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "available_at": job.available_at.isoformat() if job.available_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error_message": job.error_message,
        "result": _parse_json(job.result_json),
    }


def _serialize_open_conversation_option(*, channel: Channel, conversation: Conversation | None, chatroom: Chatroom | None) -> dict[str, Any]:
    external_id = str((conversation.external_id if conversation else None) or (chatroom.external_id if chatroom else "") or "").strip()
    title = str((conversation.title if conversation else None) or (chatroom.name if chatroom else "") or external_id).strip()
    chat_type = str((conversation.chat_type if conversation else "") or "group").strip()
    return {
        "bot_id": channel.bot_id,
        "channel_id": channel.id,
        "channel_name": channel.name,
        "channel_type": channel.channel_type,
        "conversation_id": conversation.id if conversation else None,
        "conversation_external_id": external_id,
        "conversation_title": title,
        "chat_type": chat_type,
        "chatroom_id": chatroom.id if chatroom else None,
        "chatroom_name": chatroom.name if chatroom else None,
        "chatroom_status": chatroom.status if chatroom else None,
    }


def _serialize_open_chatroom_option(*, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    external_id = str(chatroom.external_id or "").strip()
    name = str(chatroom.name or chatroom.remark or external_id).strip()
    return {
        "bot_id": channel.bot_id,
        "channel_id": channel.id,
        "channel_name": channel.name,
        "channel_type": channel.channel_type,
        "chatroom_id": chatroom.id,
        "chatroom_external_id": external_id,
        "chatroom_name": name,
        "chatroom_remark": chatroom.remark,
        "chatroom_status": chatroom.status,
        "management_status": chatroom.management_status,
        "is_active": bool(chatroom.is_active),
        "member_count": int(chatroom.member_count or 0),
    }


def _conversation_option_matches(item: dict[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    haystack = " ".join(
        str(item.get(key) or "")
        for key in (
            "conversation_external_id",
            "conversation_title",
            "chatroom_name",
            "channel_name",
            "channel_type",
            "bot_id",
            "channel_id",
            "conversation_id",
            "chatroom_id",
        )
    ).lower()
    return keyword in haystack


def _chatroom_option_matches(item: dict[str, Any], keyword: str) -> bool:
    if not keyword:
        return True
    haystack = " ".join(
        str(item.get(key) or "")
        for key in (
            "chatroom_external_id",
            "chatroom_name",
            "chatroom_remark",
            "channel_name",
            "channel_type",
            "bot_id",
            "channel_id",
            "chatroom_id",
        )
    ).lower()
    return keyword in haystack


def _authenticate_request(db: Session, request: Request, raw_body: bytes) -> OpenApiAuthResult:
    try:
        return authenticate_open_api_request(db, headers=dict(request.headers), raw_body=raw_body)
    except OpenApiAuthError as exc:
        raise HTTPException(status_code=401, detail=exc.code) from exc


def _guard_request(
    db: Session,
    *,
    app: OpenApiApp,
    request_id: str | None,
    usage: dict[str, int] | None = None,
    capability: str | None = None,
    channel_id: int | None = None,
) -> OpenApiGuardResult:
    """把统一门禁错误转换为稳定 HTTP 错误，不向外泄露 874 原始异常。"""

    try:
        return guard_open_api_request(
            db,
            app=app,
            request_id=request_id,
            usage=usage,
            capability=capability,
            channel_id=channel_id,
        )
    except OpenApiGuardError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc


def _load_authenticated_app(db: Session, credential: OpenApiAuthResult) -> OpenApiApp:
    """在租户会话中重新加载开放应用，避免认证 ORM 对象跨越会话边界。"""

    app = db.get(OpenApiApp, credential.app_id)
    if app is None or int(app.tenant_id) != credential.tenant_id:
        raise HTTPException(status_code=401, detail="app_not_found")
    return app


def _parse_json_body(raw_body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="json_object_required")
    return parsed


def _load_default_channel(db: Session, app: OpenApiApp) -> Channel:
    channel_id = int(app.default_channel_id or 0)
    if channel_id <= 0:
        raise HTTPException(status_code=400, detail="default_channel_not_configured")
    channel = db.get(Channel, channel_id)
    if channel is None or channel.channel_type not in SUPPORTED_CHANNEL_TYPES or not channel.is_enabled:
        raise HTTPException(status_code=404, detail="channel_not_found")
    return channel


def _load_authorized_channels(db: Session, app: OpenApiApp) -> list[Channel]:
    """读取开放应用授权渠道。

    一个 app_key 代表一个业务系统或客户公司，可以被授权访问多个正式渠道。
    权限只按授权渠道表判断，不再额外按 Bot 过滤；同一企业可以把多个机器人下的
    渠道授权给同一个业务系统。
    """

    rows = db.scalars(select(OpenApiAppChannel).where(OpenApiAppChannel.app_id == app.id)).all()
    channel_ids = [int(row.channel_id) for row in rows if int(row.channel_id or 0) > 0]
    if not channel_ids:
        raise HTTPException(status_code=400, detail="authorized_channels_not_configured")
    channels = db.scalars(
        select(Channel)
        .where(
            Channel.id.in_(channel_ids),
            Channel.channel_type.in_(SUPPORTED_CHANNEL_TYPES),
            Channel.is_enabled.is_(True),
        )
        .order_by(Channel.id.asc())
    ).all()
    return channels


def _resolve_open_api_channel_from_payload(db: Session, *, app: OpenApiApp, payload: dict[str, Any]) -> Channel:
    """按请求 channel_id 解析授权渠道。

    新版开放接口不再通过 default_channel_id 隐式选择渠道；收发单系统发单时
    必须明确指定供应商绑定里的 mba_channel_id，避免多企微号场景发错号。
    """

    requested_channel_id = int(payload.get("channel_id") or 0)
    if requested_channel_id <= 0:
        raise HTTPException(status_code=400, detail="channel_id_required")
    for channel in _load_authorized_channels(db, app):
        if int(channel.id or 0) == requested_channel_id:
            return channel
    raise HTTPException(status_code=403, detail="channel_not_allowed")


def _ensure_conversation(db: Session, *, channel: Channel, external_id: str, title: str, chat_type: str) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.bot_id == channel.bot_id,
            Conversation.channel_id == channel.id,
            Conversation.external_id == external_id,
        )
    )
    if conversation is None:
        conversation = Conversation(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            channel_type=channel.channel_type,
            external_id=external_id,
            title=title or external_id,
            chat_type=chat_type or "group",
        )
        db.add(conversation)
        db.flush()
    else:
        conversation.title = title or conversation.title or external_id
        conversation.chat_type = chat_type or conversation.chat_type or "group"
    return conversation


def _ensure_open_conversation_from_payload(db: Session, *, channel: Channel, payload: dict[str, Any]) -> Conversation:
    """根据开放接口请求确认 Fulcrum 会话记录。"""

    conversation_external_id = str(payload.get("conversation_external_id") or payload.get("external_id") or "").strip()
    if not conversation_external_id:
        raise HTTPException(status_code=400, detail="conversation_external_id_required")
    chat_type = str(payload.get("chat_type") or "group").strip() or "group"
    conversation_title = str(payload.get("conversation_title") or payload.get("title") or conversation_external_id).strip()
    return _ensure_conversation(
        db,
        channel=channel,
        external_id=conversation_external_id,
        title=conversation_title,
        chat_type=chat_type,
    )


def _build_open_message_payload(*, conversation: Conversation, payload: dict[str, Any], message_type: str) -> dict[str, Any]:
    """构造渠道出站消息 payload。

    这里保持字段透明传递，底层渠道适配器按自身能力取用 image_url、link_url 等字段。
    """

    content = str(payload.get("content") or payload.get("text") or "").strip()
    if message_type == "text" and not content:
        raise HTTPException(status_code=400, detail="content_required")
    outbound_payload: dict[str, Any] = {
        "external_id": conversation.external_id,
        "receiver_id": conversation.external_id,
        "chat_type": conversation.chat_type,
        "message_type": message_type,
        "content": content,
        "title": conversation.title or conversation.external_id,
    }
    optional_keys = (
        "image_url",
        "base64",
        "image_base64",
        "voice_base64",
        "audio_base64",
        "video_base64",
        "cover_base64",
        "voice_time",
        "voice_type",
        "play_length",
        "emoji",
        "card_user_id",
        "card_nickname",
        "app_type",
        "xml",
        "cdn_xml",
        "link_url",
        "link_title",
        "link_description",
        "thumb_url",
        "mini_program_appid",
        "mini_program_pagepath",
        "mini_program_title",
    )
    for key in optional_keys:
        value = payload.get(key)
        if value not in (None, ""):
            outbound_payload[key] = value
    if message_type == "image" and not outbound_payload.get("image_url"):
        if not any(outbound_payload.get(key) for key in ("base64", "image_base64", "content", "xml", "cdn_xml")):
            raise HTTPException(status_code=400, detail="image_payload_required")
    if message_type == "voice" and not any(outbound_payload.get(key) for key in ("voice_base64", "audio_base64", "base64")):
        raise HTTPException(status_code=400, detail="voice_payload_required")
    if message_type == "video" and not any(outbound_payload.get(key) for key in ("video_base64", "base64", "xml", "cdn_xml")):
        raise HTTPException(status_code=400, detail="video_payload_required")
    if message_type == "link" and not outbound_payload.get("link_url"):
        raise HTTPException(status_code=400, detail="link_url_required")
    return outbound_payload


def _resolve_open_file_url(
    *,
    tenant_id: int,
    channel_id: int,
    payload: dict[str, Any],
    file_name: str,
    public_base_url: str,
) -> tuple[str, dict[str, Any]]:
    file_url = str(payload.get("file_url") or "").strip()
    if file_url:
        return file_url, {"source": "remote_url", "file_name": file_name}

    file_bytes = _load_file_bytes_from_payload(payload)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="file_url_or_file_content_required")
    stored = store_outbound_upload(
        tenant_id=tenant_id,
        channel_id=channel_id,
        file_name=file_name,
        content=file_bytes,
        content_type=str(payload.get("content_type") or "application/octet-stream"),
        message_type="file",
    )
    download_url = f"{public_base_url}/outbound-files/{channel_id}/{stored['share_id']}/{quote(stored['file_name'])}"
    return download_url, {
        "source": "stored_upload",
        "share_id": stored["share_id"],
        "file_name": stored["file_name"],
        "file_size": stored["file_size"],
    }


def _load_file_bytes_from_payload(payload: dict[str, Any]) -> bytes:
    content_base64 = str(payload.get("file_content_base64") or "").strip()
    if content_base64:
        try:
            return base64.b64decode(content_base64)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_file_content_base64") from exc
    file_path = str(payload.get("file_path") or "").strip()
    if file_path:
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=400, detail="file_path_not_found")
        return path.read_bytes()
    return b""


def _parse_json(raw: str | None) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
