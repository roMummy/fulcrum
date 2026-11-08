"""开放消息查询接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.open_api import _authenticate_request, _guard_request, _load_authenticated_app
from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Conversation, Message, OpenApiAppChannel
from app.services.open_api_quotas import finalize_open_api_usage


router = APIRouter(tags=["open-api-messages"])


def _message_view(message: Message, conversation: Conversation) -> dict[str, Any]:
    """把内部消息模型转换为稳定业务消息对象。"""

    return {
        "id": int(message.id),
        "external_id": message.external_message_id,
        "channel_id": int(conversation.channel_id or 0),
        "conversation_external_id": conversation.external_id,
        "conversation_title": conversation.title,
        "chat_type": conversation.chat_type,
        "direction": message.direction,
        "message_type": message.message_type,
        "content": message.content,
        "status": message.status,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


@router.get("/api/open/messages/sync")
async def open_messages_sync_api(
    request: Request,
    channel_id: int | None = None,
    cursor: int = 0,
    limit: int = 100,
):
    """按内部消息主键游标读取开放应用已授权渠道的消息增量。"""

    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            usage={"api_requests": 1},
            capability="message.read",
            channel_id=channel_id,
        )
        channel_ids = [int(channel_id)] if channel_id else [
            int(value)
            for value in db.scalars(
                select(OpenApiAppChannel.channel_id).where(OpenApiAppChannel.app_id == int(app.id))
            ).all()
        ]
        safe_limit = min(max(1, int(limit or 100)), 200)
        rows = db.execute(
            select(Message, Conversation)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.channel_id.in_(channel_ids),
                Message.id > max(0, int(cursor or 0)),
            )
            .order_by(Message.id.asc())
            .limit(safe_limit)
        ).all()
        items = [_message_view(message, conversation) for message, conversation in rows]
        result = {"ok": True, "items": items, "next_cursor": str(rows[-1][0].id) if rows else None}
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result

