"""微信 iOS 群实体落库的测试。

用独立的 SQLite 内存库验证：群消息建群 + 落名册成员、直聊忽略、重复消息不重复建成员。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel, Chatroom, ChatroomMember
from app.services import wechat_gugatool_chatrooms
from app.services.wechat_gugatool_chatrooms import sync_wechat_gugatool_chatroom_from_message as sync_chatroom
from app.services.wechat_gugatool_chatrooms import _resolve_chatroom_name, _resolve_seen_at


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'chatrooms.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _channel(db) -> Channel:
    channel = Channel(bot_id=1, name="咕嘎助手", channel_type="wechat_gugatool", api_base="http://127.0.0.1:19090", tenant_id=7)
    db.add(channel)
    db.flush()
    return channel


def _normalized(room: str = "1@chatroom", *, chat_type: str = "group", sender: str = "wxid_a") -> dict:
    return {
        "event_key": f"evt:{room}",
        "conversation": {"external_id": room, "chat_type": chat_type, "title": room},
        "message": {"sender_id": sender, "message_type": "text"},
        "details": {"create_time": 1794362355},
    }


def test_group_message_creates_chatroom_and_roster_members(monkeypatch, session_factory) -> None:
    roster = {
        "title": "峡谷玩具车",
        "members": {
            "wxid_owner": {"wxid": "wxid_owner", "name": "芒果", "display_name": "芒果与酒", "is_owner": True},
            "wxid_a": {"wxid": "wxid_a", "name": "老王", "display_name": "群最高雅的人", "is_admin": True},
        },
    }
    monkeypatch.setattr(wechat_gugatool_chatrooms, "cached_group", lambda *args: roster)

    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        result = sync_chatroom(db, channel, _normalized())
        db.commit()

        assert result["chatroom_created"] is True
        assert result["member_created"] == 2

        chatroom = db.scalar(select(Chatroom).where(Chatroom.external_id == "1@chatroom"))
        assert chatroom.name == "峡谷玩具车"
        assert chatroom.management_status == "enabled"
        assert chatroom.is_active is True
        assert (chatroom.first_seen_at is not None) and (chatroom.last_seen_at is not None)

        members = db.scalars(select(ChatroomMember).order_by(ChatroomMember.member_external_id)).all()
        assert [m.member_external_id for m in members] == ["wxid_a", "wxid_owner"]
        assert members[0].display_name == "群最高雅的人"
        assert members[0].role == "admin"
        assert members[1].role == "owner"


def test_direct_message_is_ignored(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        result = sync_chatroom(db, channel, _normalized("wxid_peer", chat_type="direct", sender="wxid_peer"))
        db.commit()

        assert result["chatroom_created"] is False
        assert db.scalar(select(Chatroom)) is None


def test_second_message_does_not_duplicate_roster(monkeypatch, session_factory) -> None:
    roster = {
        "title": "群",
        "members": {
            "wxid_a": {"wxid": "wxid_a", "name": "老王"},
            "wxid_b": {"wxid": "wxid_b", "name": "小李"},
        },
    }
    monkeypatch.setattr(wechat_gugatool_chatrooms, "cached_group", lambda *args: roster)

    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        sync_chatroom(db, channel, _normalized())
        db.commit()
        # 第二条消息只更新发送者，不再遍历名册
        result = sync_chatroom(db, channel, _normalized(sender="wxid_b"))
        db.commit()

        assert result["chatroom_created"] is False
        assert result["member_created"] == 0
        assert db.query(ChatroomMember).count() == 2


def test_chatroom_name_prefers_roster_over_technical_id() -> None:
    assert _resolve_chatroom_name({"title": "真群名"}, {"title": "1@chatroom"}, None, "1@chatroom") == "真群名"
    assert _resolve_chatroom_name({}, {"title": "1@chatroom"}, None, "1@chatroom") == "1@chatroom"


def test_seen_at_falls_back_to_now_for_invalid_timestamp() -> None:
    assert _resolve_seen_at({"create_time": "bad"}) is not None
    assert _resolve_seen_at({"create_time": 1794362355}).year == 2026
