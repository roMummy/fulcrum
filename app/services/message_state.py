from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.entities import Conversation, Message


def refresh_conversation_last_message_at(db: Session, conversation_id: int) -> None:
    latest_message_at = (
        db.scalar(select(func.max(Message.created_at)).where(Message.conversation_id == conversation_id))
        or None
    )
    db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(last_message_at=latest_message_at)
    )
