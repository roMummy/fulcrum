"""各渠道"消息 → 建群"入口的回归:首次建群、重复消息不重复、并发只建一个、已有设置不被冲掉。

背景:建群逻辑改为统一的并发安全入口(chatroom_directory)后,iPad、Windows 微信、企业微信、咕嘎、微桥都改了。
winauto/企微的既有测试夹具有历史问题(造机器人时缺租户上下文,在测试开头就失败),它们的建群代码
长期没有任何测试跑到;这里在正确的租户上下文里直接调用各渠道的真实入口,补上这块覆盖。
"""

from __future__ import annotations

import os
import tempfile
import threading

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="channel-room-logs-"))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Bot, Channel, Chatroom, Conversation
from app.models.saas import Tenant
from app.services.wechat_gugatool_chatrooms import sync_wechat_gugatool_chatroom_from_message
from app.services.wechat_ipad.chatrooms import sync_wechat_ipad_chatroom_from_message
from app.services.wechat_winauto_chatrooms import sync_wechat_winauto_chatroom_from_message
from app.services.workwechat_chatrooms import sync_workwechat_chatroom_from_message

# (渠道类型, 入口函数)
ENTRIES = [
    ("wechat_ipad", sync_wechat_ipad_chatroom_from_message),
    ("wechat_winauto", sync_wechat_winauto_chatroom_from_message),
    ("workwechat", sync_workwechat_chatroom_from_message),
    ("wechat_gugatool", sync_wechat_gugatool_chatroom_from_message),
]


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rooms.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id=7, code="t", name="t"))
        db.commit()
    return factory


def _channel(db, channel_type: str) -> Channel:
    bot = Bot(name=f"bot-{channel_type}", tenant_id=7)
    db.add(bot)
    db.flush()
    channel = Channel(bot_id=bot.id, name=channel_type, channel_type=channel_type, is_enabled=True, tenant_id=7)
    db.add(channel)
    db.flush()
    return channel


def _event(room: str, sender: str = "wxid_a") -> dict:
    return {
        "conversation": {"external_id": room, "chat_type": "group", "title": "测试群"},
        "message": {"sender_id": sender, "sender_name": "甲", "content": "hi", "message_type": "text"},
        "details": {},
    }


def _conversation(db, channel: Channel, room: str) -> Conversation:
    conversation = Conversation(tenant_id=7, bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type, external_id=room, chat_type="group", title="测试群")
    db.add(conversation)
    db.flush()
    return conversation


@pytest.mark.parametrize("channel_type, entry", ENTRIES, ids=[item[0] for item in ENTRIES])
def test_first_message_creates_the_group_and_repeats_do_not_duplicate_it(env, channel_type, entry) -> None:
    room = "first@chatroom"
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, channel_type)
        conversation = _conversation(db, channel, room)
        first = entry(db, channel, _event(room), conversation=conversation)
        second = entry(db, channel, _event(room, "wxid_b"), conversation=conversation)
        db.commit()
        assert first["chatroom_created"] is True and second["chatroom_created"] is False
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == room)) == 1
        assert first["chatroom_id"] == second["chatroom_id"] and first["chatroom_id"]


@pytest.mark.parametrize("channel_type, entry", ENTRIES, ids=[item[0] for item in ENTRIES])
def test_concurrent_first_messages_never_duplicate_the_group(env, channel_type, entry) -> None:
    room = "race@chatroom"
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, channel_type)
        _conversation(db, channel, room)
        db.commit()
        channel_id = int(channel.id)
    barrier = threading.Barrier(6)
    errors, created = [], []

    def run(index: int) -> None:
        try:
            with tenant_scope(7), open_tenant_session(env) as db:
                channel = db.get(Channel, channel_id)
                conversation = db.scalar(select(Conversation).where(Conversation.external_id == room))
                barrier.wait()
                result = entry(db, channel, _event(room, f"wxid_{index}"), conversation=conversation)
                db.commit()
                created.append(result["chatroom_created"])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(index,)) for index in range(6)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert errors == [], errors
    with tenant_scope(7), open_tenant_session(env) as db:
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == room)) == 1
    assert created.count(True) == 1  # 只有一个请求被告知"这个群是你建的"——据此才会做首次启用/初始化


@pytest.mark.parametrize("channel_type, entry", ENTRIES, ids=[item[0] for item in ENTRIES])
def test_an_existing_groups_settings_survive_later_messages(env, channel_type, entry) -> None:
    room = "keep@chatroom"
    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, channel_type)
        conversation = _conversation(db, channel, room)
        entry(db, channel, _event(room), conversation=conversation)
        chatroom = db.scalar(select(Chatroom).where(Chatroom.external_id == room))
        chatroom.management_status = "archived"  # 管理员后来把它设成"完全关闭"
        chatroom.operator_note = "管理员备注"
        entry(db, channel, _event(room, "wxid_c"), conversation=conversation)
        db.commit()
        stored = db.scalar(select(Chatroom).where(Chatroom.external_id == room))
        assert stored.operator_note == "管理员备注"  # 后续消息不会把用户的设置冲掉


def test_the_manual_registration_path_is_idempotent(env) -> None:
    from app.services.chatroom_registry import upsert_manual_chatroom

    with tenant_scope(7), open_tenant_session(env) as db:
        channel = _channel(db, "wechat_ipad")
        first = upsert_manual_chatroom(db, channel=channel, external_id="manual@chatroom", name="手动群")
        again = upsert_manual_chatroom(db, channel=channel, external_id="manual@chatroom", remark="备注")
        db.commit()
        assert first.id == again.id and again.remark == "备注" and again.name == "手动群"
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == "manual@chatroom")) == 1
