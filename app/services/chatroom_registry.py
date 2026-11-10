from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom


def mark_chatroom_discovered(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    seen_at: datetime | None = None,
    registration_source: str = "passive",
) -> Chatroom:
    resolved_seen_at = _normalize_dt(seen_at)
    if not getattr(chatroom, "first_seen_at", None):
        chatroom.first_seen_at = resolved_seen_at
    chatroom.last_seen_at = resolved_seen_at
    chatroom.registration_source = str(getattr(chatroom, "registration_source", None) or registration_source or "passive")
    if not chatroom.bot_id:
        chatroom.bot_id = channel.bot_id
    if not chatroom.channel_id:
        chatroom.channel_id = channel.id
    return chatroom


def upsert_manual_chatroom(
    db: Session,
    *,
    channel: Channel,
    external_id: str,
    name: str | None = None,
    remark: str | None = None,
    operator_note: str | None = None,
) -> Chatroom:
    normalized_external_id = str(external_id or "").strip()
    if not normalized_external_id:
        raise ValueError("external_id is required")
    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == normalized_external_id)
    )
    if not chatroom:
        chatroom = Chatroom(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            external_id=normalized_external_id,
            status="pending_sync",
        )
        db.add(chatroom)
        db.flush()
    mark_chatroom_discovered(db, channel=channel, chatroom=chatroom, registration_source="manual")
    chatroom.registration_source = "manual"
    if name is not None:
        chatroom.name = str(name or "").strip() or normalized_external_id
    elif not chatroom.name:
        chatroom.name = normalized_external_id
    if remark is not None:
        chatroom.remark = str(remark or "").strip() or None
    if operator_note is not None:
        chatroom.operator_note = str(operator_note or "").strip() or None
    return chatroom


def _normalize_dt(value: datetime | None) -> datetime:
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    return now_shanghai()
