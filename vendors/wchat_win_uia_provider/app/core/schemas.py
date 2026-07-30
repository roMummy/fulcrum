from __future__ import annotations

from pydantic import BaseModel, Field


class SwitchSessionRequest(BaseModel):
    session_title: str = Field(min_length=1, max_length=255)


class SwitchFirstUnreadRequest(BaseModel):
    chat_type_hint: str | None = Field(default=None, max_length=32)


class FetchFirstUnreadIncrementalRequest(BaseModel):
    chat_type_hint: str | None = Field(default=None, max_length=32)
    switch_wait_seconds: float = Field(default=2.0, ge=0.0, le=10.0)
    mark_as_seen: bool = True


class SendTextRequest(BaseModel):
    session_title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)


class SendFileRequest(BaseModel):
    session_title: str = Field(min_length=1, max_length=255)
    file_path: str = Field(min_length=1)


class WatcherControlResponse(BaseModel):
    ok: bool
    status: str
    summary: str
