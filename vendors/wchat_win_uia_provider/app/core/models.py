from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RectModel(BaseModel):
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0


class SessionPreviewModel(BaseModel):
    session_id: str
    title: str
    preview: str | None = None
    timestamp_text: str | None = None
    unread_hint: str | None = None
    has_new: bool = False
    chat_type_hint: str | None = None
    rect: RectModel | None = None
    raw_text: str | None = None
    auto_id: str | None = None


class ConversationModel(BaseModel):
    conversation_id: str
    title: str
    chat_type: Literal["direct", "group", "unknown"] = "unknown"
    member_count_hint: str | None = None


class MessageModel(BaseModel):
    message_id: str
    message_type: str = "text"
    content: str = ""
    sender_name: str | None = None
    sender_id: str | None = None
    direction: Literal["in", "out", "unknown"] = "unknown"
    rect: RectModel | None = None
    raw_control_meta: dict[str, Any] = Field(default_factory=dict)


class ProviderEventModel(BaseModel):
    event_id: str
    event_type: str
    created_at: datetime = Field(default_factory=utc_now)
    conversation: ConversationModel | None = None
    message: MessageModel | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SnapshotModel(BaseModel):
    ok: bool = True
    provider: str = "wechat_windows_uia"
    generated_at: datetime = Field(default_factory=utc_now)
    conversation: ConversationModel | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    chats: list[SessionPreviewModel] = Field(default_factory=list)
    messages: list[MessageModel] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class WatcherStateModel(BaseModel):
    updated_at: datetime = Field(default_factory=utc_now)
    fingerprint: dict[str, dict[str, Any]] = Field(default_factory=dict)
    count: int = 0


class MessageCursorChatStateModel(BaseModel):
    updated_at: datetime = Field(default_factory=utc_now)
    seen_message_ids: list[str] = Field(default_factory=list)
    seen_fingerprints: list[str] = Field(default_factory=list)


class MessageCursorStateModel(BaseModel):
    updated_at: datetime = Field(default_factory=utc_now)
    chats: dict[str, MessageCursorChatStateModel] = Field(default_factory=dict)
