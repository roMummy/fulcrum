"""群的三档运行模式:正常运行 / 只采集消息 / 完全关闭 的入站拦截与状态映射。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="group-mode-logs-"))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, Chatroom, Conversation, Message, RawWebhookEvent
from app.services import channel_events
from app.services.chatroom_registry import (
    GROUP_MODE_COLLECT_ONLY,
    GROUP_MODE_OFF,
    GROUP_MODE_OPTIONS,
    GROUP_MODE_RUN,
    is_collection_off,
    set_chatroom_management_status,
)

ROOM = "g@chatroom"


def _event(key: str, chat_type: str = "group", external_id: str = ROOM) -> dict:
    return {
        "event_key": key,
        "should_persist_message": True,
        "conversation": {"external_id": external_id, "chat_type": chat_type, "title": "群"},
        "message": {"message_type": "text", "content": "你好", "sender_id": "wxid_a", "sender_name": "小明"},
        "details": {},
    }


@pytest.fixture()
def env():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge", status="online", is_enabled=True)
            db.add(channel)
            db.flush()
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id=ROOM, name="群")
            db.add(room)
            db.flush()
            yield db, channel, room


def _counts(db) -> tuple[int, int, int]:
    return tuple(int(db.scalar(select(func.count()).select_from(model)) or 0) for model in (RawWebhookEvent, Conversation, Message))


def test_mode_switch_maps_to_status_and_dispatch_switch(env) -> None:
    db, channel, room = env
    set_chatroom_management_status(room, GROUP_MODE_RUN)
    assert room.is_active is True and not is_collection_off(room)
    set_chatroom_management_status(room, GROUP_MODE_COLLECT_ONLY)
    assert room.is_active is False and not is_collection_off(room)
    set_chatroom_management_status(room, GROUP_MODE_OFF)
    assert room.is_active is False and is_collection_off(room)
    assert [item["value"] for item in GROUP_MODE_OPTIONS] == [GROUP_MODE_RUN, GROUP_MODE_COLLECT_ONLY, GROUP_MODE_OFF]


def test_fully_closed_group_drops_inbound_events(env) -> None:
    db, channel, room = env
    set_chatroom_management_status(room, GROUP_MODE_OFF)
    result = channel_events.ingest_normalized_event(db, channel, _event("e1"))
    assert result["ignored"] is True and result["raw_event_id"] is None and result["mapped"] is False
    # 什么都不落库:原始事件、会话、消息都没有
    assert _counts(db) == (0, 0, 0)


def test_collect_only_group_still_records_messages_without_dispatch(env, monkeypatch) -> None:
    db, channel, room = env
    set_chatroom_management_status(room, GROUP_MODE_COLLECT_ONLY)
    result = channel_events.ingest_normalized_event(db, channel, _event("e2"))
    assert not result.get("ignored")
    raw, conversations, messages = _counts(db)
    assert raw == 1 and conversations == 1 and messages == 1  # 消息照常采集


def test_other_groups_and_private_chats_are_not_affected(env) -> None:
    db, channel, room = env
    set_chatroom_management_status(room, GROUP_MODE_OFF)
    # 同渠道的另一个群、私聊都照常
    assert not channel_events.ingest_normalized_event(db, channel, _event("e3", external_id="other@chatroom")).get("ignored")
    assert not channel_events.ingest_normalized_event(db, channel, _event("e4", chat_type="direct", external_id=ROOM)).get("ignored")
