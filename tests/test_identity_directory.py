"""消息身份使用已同步的本群资料，原始 ID 不遮挡昵称，头像独立回退。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, Message, WorkWechatContact
from app.services.identity_directory import IdentityDirectory
from app.services.wechat_webridge.service import persist_directory_result


@pytest.fixture()
def directory_env():
    engine = create_engine("sqlite://")
    for model in (Channel, Chatroom, ChatroomMember, Conversation, Message, WorkWechatContact):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1), tenant_session(factory) as db:
        channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge")
        db.add(channel)
        db.flush()
        rooms, conversations, messages = [], [], []
        for number in range(2):
            ext = f"room{number}@chatroom"
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id=ext, name="测试群")
            conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type=channel.channel_type, external_id=ext, chat_type="group")
            db.add_all([room, conversation])
            db.flush()
            message = Message(tenant_id=1, conversation_id=conversation.id, direction="in", sender_id="wxid_a1", sender_name="wxid_a1", content="历史消息", message_type="text")
            db.add(message)
            rooms.append(room)
            conversations.append(conversation)
            messages.append(message)
        db.flush()
        yield db, channel, rooms, conversations, messages
    engine.dispose()


def add_member(env, index=0, **profile):
    db, channel, rooms, _, _ = env
    member = ChatroomMember(tenant_id=1, bot_id=1, channel_id=channel.id, chatroom_id=rooms[index].id, member_external_id="wxid_a1", **profile)
    db.add(member)
    db.flush()
    return member


def test_first_sync_enriches_existing_messages_without_rewriting_them(directory_env):
    db, channel, rooms, _, messages = directory_env
    message = messages[0]
    assert IdentityDirectory(db).for_messages([message])[message.id]["name"] == "wxid_a1"
    persist_directory_result(db, channel, {"type": "room_members", "params": {"room_id": rooms[0].external_id}}, {
        "members": [{"account_id": "wxid_a1", "nick_name": "小明", "room_nick_name": "wxid_a1", "avatar_url": "https://example.com/member.jpg"}],
    })
    who = IdentityDirectory(db).for_messages([message])[message.id]
    assert who["name"] == "小明" and who["avatar_url"] == "https://example.com/member.jpg"
    assert who["external_id"] == "wxid_a1" and who["show_id"] is True
    assert message.sender_name == "wxid_a1"


def test_group_context_and_cache_keep_each_groups_nickname(directory_env):
    db, channel, _, conversations, messages = directory_env
    add_member(directory_env, display_name="甲群昵称", extra_json=json.dumps({"avatar_url": "https://example.com/a.jpg"}))
    add_member(directory_env, index=1, display_name="乙群昵称")
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_external_id="wxid_a1", display_name="好友备注", avatar_url="https://example.com/friend.jpg"))
    db.flush()
    directory = IdentityDirectory(db)
    # 先预加载无群上下文的好友资料，也不能污染随后的本群查询。
    assert directory.get(channel.id, "wxid_a1")["name"] == "好友备注"
    identities = directory.for_messages(messages)
    assert identities[messages[0].id]["name"] == "甲群昵称"
    assert identities[messages[0].id]["avatar_url"] == "https://example.com/a.jpg"
    assert identities[messages[1].id]["name"] == "乙群昵称"
    assert identities[messages[1].id]["avatar_url"] == "https://example.com/friend.jpg"
    conversations[0].chat_type = "direct"
    db.flush()
    assert directory.for_messages([messages[0]])[messages[0].id]["name"] == "好友备注"


def test_another_groups_profile_is_not_used_for_missing_member(directory_env):
    db, _, _, _, messages = directory_env
    add_member(directory_env, index=1, display_name="别群昵称", extra_json=json.dumps({"avatar_url": "https://example.com/wrong.jpg"}))
    messages[0].sender_name = "消息自带昵称"
    who = IdentityDirectory(db).for_messages([messages[0]])[messages[0].id]
    assert who["name"] == "消息自带昵称" and who["avatar_url"] == ""


def test_avatar_only_member_keeps_message_name_and_left_member_keeps_profile(directory_env):
    db, _, _, _, messages = directory_env
    member = add_member(directory_env, display_name="wxid_a1", extra_json=json.dumps({"avatar_url": "https://example.com/member.jpg"}))
    messages[0].sender_name = "消息昵称"
    who = IdentityDirectory(db).for_messages([messages[0]])[messages[0].id]
    assert who["name"] == "消息昵称" and who["avatar_url"] == "https://example.com/member.jpg"
    member.nickname, member.status = "已退群的成员", "left"
    db.flush()
    assert IdentityDirectory(db).for_messages([messages[0]])[messages[0].id]["name"] == "已退群的成员"


def test_contact_raw_display_name_does_not_hide_name_or_erase_member_avatar(directory_env):
    db, channel, _, _, _ = directory_env
    add_member(directory_env, nickname="成员昵称", extra_json=json.dumps({"avatar_url": "https://example.com/member.jpg"}))
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_external_id="wxid_a1", display_name="wxid_a1", name="好友昵称"))
    db.flush()
    who = IdentityDirectory(db).get(channel.id, "wxid_a1")
    assert who["name"] == "好友昵称" and who["avatar_url"] == "https://example.com/member.jpg"


def test_empty_batch_and_system_outbound_sender(directory_env):
    db, _, _, _, messages = directory_env
    directory = IdentityDirectory(db)
    assert directory.for_messages([]) == {}
    messages[0].direction, messages[0].sender_id = "out", "outbound-adapter:test"
    who = directory.for_messages([messages[0]])[messages[0].id]
    # 自己发出的消息统一显示渠道账号资料(没有账号昵称时用渠道名),并标记 is_own;
    # 关键性质不变:不暴露 outbound-adapter 之类的内部发送者标识,也不显示 ID 行。
    assert who["name"] == "微桥" and who["is_own"] is True and who["show_id"] is False
    assert "outbound-adapter" not in str(who)
