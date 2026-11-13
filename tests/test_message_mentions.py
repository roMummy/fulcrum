"""群 @ 选择必须包含未发言成员，并严格校验群范围与离群状态。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, Conversation, Message, WorkWechatContact
from app.models.saas import Tenant
from app.services.message_mentions import ALL_ID, can_mention_all, members, validate_members
from tests.test_ai_chat import _session


OWN = "wxid_bot"


@pytest.fixture()
def mention_env():
    db = _session()
    db.add(Tenant(id=1, code="mentions", name="群成员测试"))
    bot = Bot(tenant_id=1, name="机器人")
    db.add(bot)
    db.flush()
    channel = Channel(tenant_id=1, bot_id=bot.id, channel_type="wechat_webridge", name="微桥账号",
                      runtime_info_json=json.dumps({"webridge": {"account_id": OWN}}))
    db.add(channel)
    db.flush()
    conversation = Conversation(tenant_id=1, bot_id=bot.id, channel_id=channel.id,
                                channel_type=channel.channel_type, external_id="current@chatroom", chat_type="group")
    db.add(conversation)
    db.commit()
    yield db, channel, conversation
    db.close()


def _room(db, channel, external_id="current@chatroom", *, owner=""):
    room = Chatroom(tenant_id=1, bot_id=channel.bot_id, channel_id=channel.id, external_id=external_id, name="群", owner_external_id=owner)
    db.add(room)
    db.commit()
    return room


def _member(db, room, external_id="wxid_member", *, group_name="群昵称", nickname="微信昵称", status="active", avatar="", role="member"):
    member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id,
                                                   ChatroomMember.member_external_id == external_id))
    if member is None:
        member = ChatroomMember(tenant_id=1, bot_id=room.bot_id, channel_id=room.channel_id, chatroom_id=room.id,
                                member_external_id=external_id)
        db.add(member)
    # 成员目录具有唯一约束，离群或降权应更新同一记录，而不是插入历史副本。
    member.display_name, member.nickname = group_name, nickname
    member.status, member.role, member.extra_json = status, role, json.dumps({"avatar_url": avatar})
    db.commit()
    return member


def _message(db, conversation, external_id, name="消息昵称", *, direction="in", status="received"):
    db.add(Message(tenant_id=1, conversation_id=conversation.id, direction=direction, sender_id=external_id,
                   sender_name=name, content="旧消息", message_type="text", status=status))
    db.commit()


def _ids(result):
    return [item["id"] for item in result["members"]]


def test_directory_includes_members_who_never_spoke_and_prefers_group_identity(mention_env):
    db, channel, conversation = mention_env
    room = _room(db, channel)
    _member(db, room, avatar="https://avatars.test/group.jpg")
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_external_id="wxid_member",
                            display_name="好友备注", name="联系人昵称", avatar_url="https://avatars.test/contact.jpg"))
    db.commit()
    result = members(db, channel, conversation)
    assert _ids(result) == ["wxid_member"] and not result["has_more"]
    assert result["members"][0] == {"id": "wxid_member", "name": "群昵称", "avatar_url": "https://avatars.test/group.jpg", "initial": "群"}
    assert validate_members(db, channel, conversation, ["wxid_member"]) == [{"id": "wxid_member", "name": "群昵称"}]


@pytest.mark.parametrize("query", ["群昵称", "微信昵称", "好友备注", "联系人昵称", "wxid_member"])
def test_search_covers_group_name_nickname_friend_remark_and_id(mention_env, query):
    db, channel, conversation = mention_env
    room = _room(db, channel)
    _member(db, room)
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_external_id="wxid_member",
                            display_name="好友备注", name="联系人昵称"))
    db.commit()
    result = members(db, channel, conversation, query=query)
    assert _ids(result) == ["wxid_member"]
    assert result["members"][0]["name"] == "群昵称"


def test_member_name_and_avatar_can_fall_back_to_own_channel_contact(mention_env):
    db, channel, conversation = mention_env
    room = _room(db, channel)
    _member(db, room, group_name="wxid_member", nickname="")
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_external_id="wxid_member",
                            display_name="好友备注", avatar_url="https://avatars.test/contact.jpg"))
    db.commit()
    item = members(db, channel, conversation, query="好友备注")["members"][0]
    assert item["name"] == "好友备注" and item["avatar_url"] == "https://avatars.test/contact.jpg"


def test_left_other_group_other_channel_and_self_are_excluded(mention_env):
    db, channel, conversation = mention_env
    room = _room(db, channel)
    _member(db, room, "wxid_active")
    _member(db, room, "wxid_left", status="left")
    _member(db, room, OWN)
    other_room = _room(db, channel, "another@chatroom")
    _member(db, other_room, "wxid_other_group")
    other_channel = Channel(tenant_id=1, bot_id=channel.bot_id, channel_type="wechat_webridge", name="另一账号")
    db.add(other_channel)
    db.commit()
    other_channel_room = _room(db, other_channel)
    _member(db, other_channel_room, "wxid_other_channel")
    for external_id in ("wxid_left", "wxid_other_group", "wxid_other_channel"):
        _message(db, conversation, external_id)
    assert _ids(members(db, channel, conversation)) == ["wxid_active"]
    for external_id in ("wxid_left", "wxid_other_group", "wxid_other_channel", OWN):
        with pytest.raises(ValueError, match="不属于当前群聊"):
            validate_members(db, channel, conversation, [external_id])


def test_only_left_directory_never_resurrects_old_message_senders(mention_env):
    db, channel, conversation = mention_env
    room = _room(db, channel)
    _member(db, room, "wxid_left", status="left")
    _message(db, conversation, "wxid_left")
    assert _ids(members(db, channel, conversation)) == []
    with pytest.raises(ValueError, match="已离群"):
        validate_members(db, channel, conversation, ["wxid_left"])


def test_updated_member_departure_removes_mention_candidate(mention_env):
    db, channel, conversation = mention_env
    room = _room(db, channel)
    _member(db, room)
    assert _ids(members(db, channel, conversation)) == ["wxid_member"]
    _member(db, room, status="left")
    assert _ids(members(db, channel, conversation)) == []
    with pytest.raises(ValueError, match="已离群"):
        validate_members(db, channel, conversation, ["wxid_member"])


@pytest.mark.parametrize("with_room", [False, True], ids=["no-group-record", "group-without-member-records"])
def test_legacy_group_falls_back_to_current_conversation_senders_only(mention_env, with_room):
    db, channel, conversation = mention_env
    if with_room:
        _room(db, channel)
    _message(db, conversation, "wxid_speaker", "旧昵称")
    _message(db, conversation, "wxid_speaker", "最新昵称")
    _message(db, conversation, OWN, "本人历史入站")
    _message(db, conversation, "outbound-adapter:wechat_webridge:1", direction="out")
    _message(db, conversation, "admin-console")
    other = Conversation(tenant_id=1, bot_id=channel.bot_id, channel_id=channel.id,
                         channel_type=channel.channel_type, external_id="other@chatroom", chat_type="group")
    db.add(other)
    db.commit()
    _message(db, other, "wxid_outsider")
    result = members(db, channel, conversation)
    assert _ids(result) == ["wxid_speaker"]
    assert result["members"][0]["name"] == "最新昵称"
    assert validate_members(db, channel, conversation, ["wxid_speaker"]) == [{"id": "wxid_speaker", "name": "最新昵称"}]
    with pytest.raises(ValueError, match="不属于"):
        validate_members(db, channel, conversation, ["wxid_outsider"])


def test_all_uses_protocol_marker_and_is_searchable_without_expanding_members(mention_env):
    db, channel, conversation = mention_env
    _room(db, channel, owner=OWN)
    result = members(db, channel, conversation, query="@所有")
    assert result["members"] == [{"id": "notify@all", "name": "所有人", "avatar_url": "", "initial": "全"}]
    assert validate_members(db, channel, conversation, [ALL_ID]) == [{"id": ALL_ID, "name": "所有人"}]


@pytest.mark.parametrize("kind,chat_type,capabilities", [("wechat_webridge", "direct", None),
                                                       ("workwechat", "group", None),
                                                       ("wechat_webridge", "group", ["send_image"])])
def test_direct_and_unsupported_channels_have_no_candidates_and_reject_mentions(mention_env, kind, chat_type, capabilities):
    db, channel, conversation = mention_env
    channel.channel_type = conversation.channel_type = kind
    conversation.chat_type = chat_type
    if capabilities is not None:
        channel.runtime_info_json = json.dumps({"webridge": {"account_id": OWN, "capabilities": capabilities}})
    db.commit()
    assert members(db, channel, conversation) == {"members": [], "has_more": False}
    with pytest.raises(ValueError, match="只有群主或管理员才能 @所有人"):
        validate_members(db, channel, conversation, [ALL_ID])


def test_ipad_supports_regular_members_but_not_unverified_all_marker(mention_env):
    db, channel, conversation = mention_env
    channel.channel_type = conversation.channel_type = "wechat_ipad"
    room = _room(db, channel)
    _member(db, room)
    assert _ids(members(db, channel, conversation)) == ["wxid_member"]
    assert validate_members(db, channel, conversation, ["wxid_member"])[0]["id"] == "wxid_member"
    with pytest.raises(ValueError, match="只有群主或管理员才能 @所有人"):
        validate_members(db, channel, conversation, [ALL_ID])


def test_limit_has_more_and_validation_are_independent_of_visible_page(mention_env):
    db, channel, conversation = mention_env
    room = _room(db, channel)
    _member(db, room, OWN, role="admin")
    for index in range(4):
        _member(db, room, f"wxid_{index}", group_name=f"成员{index}")
    result = members(db, channel, conversation, limit=2)
    assert len(result["members"]) == 2 and result["has_more"]
    assert validate_members(db, channel, conversation, ["wxid_3", ALL_ID, "wxid_3"]) == [
        {"id": "wxid_3", "name": "成员3"}, {"id": ALL_ID, "name": "所有人"}]
    assert not members(db, channel, conversation, query="不存在", limit=2)["has_more"]


@pytest.mark.parametrize("selected", [None, "wxid_member", [""], [1], ["x" * 129], ["x"] * 21])
def test_invalid_selections_are_rejected(mention_env, selected):
    db, channel, conversation = mention_env
    with pytest.raises(ValueError, match="参数无效"):
        validate_members(db, channel, conversation, selected)


def test_mismatched_channel_does_not_expose_or_validate_members(mention_env):
    db, channel, conversation = mention_env
    conversation.channel_id = channel.id + 1
    db.commit()
    assert members(db, channel, conversation) == {"members": [], "has_more": False}
    with pytest.raises(ValueError, match="只有群主或管理员才能 @所有人"):
        validate_members(db, channel, conversation, [ALL_ID])


@pytest.mark.parametrize("source", ["room-owner", "member-owner", "member-admin"])
def test_all_requires_confirmed_current_account_owner_or_admin(mention_env, source):
    db, channel, conversation = mention_env
    room = _room(db, channel, owner=OWN if source == "room-owner" else "wxid_other_owner")
    if source != "room-owner":
        _member(db, room, OWN, role="owner" if source == "member-owner" else "admin")
    assert can_mention_all(db, channel, conversation)
    assert ALL_ID in _ids(members(db, channel, conversation))
    assert validate_members(db, channel, conversation, [ALL_ID]) == [{"id": ALL_ID, "name": "所有人"}]


@pytest.mark.parametrize("condition", ["missing-room", "missing-self", "ordinary-member", "unknown-identity",
                                      "left-self-admin", "left-self-owner", "left-room", "unknown-role",
                                      "updated-self-is-left", "demoted-self-is-member"])
def test_all_is_hidden_and_rejected_without_current_authority(mention_env, condition):
    db, channel, conversation = mention_env
    if condition != "missing-room":
        room = _room(db, channel, owner=OWN if condition in {"left-self-owner", "unknown-identity"} else "wxid_other_owner")
        if condition != "missing-self":
            _member(db, room, OWN, role="member" if condition == "ordinary-member" else (None if condition == "unknown-role" else "admin"),
                    status="left" if condition in {"left-self-admin", "left-self-owner"} else "active")
        if condition == "left-room":
            room.status = "left"
        if condition in {"updated-self-is-left", "demoted-self-is-member"}:
            assert can_mention_all(db, channel, conversation)
            _member(db, room, OWN, role="member", status="left" if condition == "updated-self-is-left" else "active")
        if condition == "unknown-identity":
            channel.runtime_info_json = "{}"
        db.commit()
    assert not can_mention_all(db, channel, conversation)
    assert ALL_ID not in _ids(members(db, channel, conversation))
    with pytest.raises(ValueError, match="只有群主或管理员才能 @所有人"):
        validate_members(db, channel, conversation, [ALL_ID])


@pytest.mark.parametrize("mismatch", ["room", "channel", "room-tenant", "member-tenant", "room-bot", "member-bot", "conversation-tenant"])
def test_privileged_role_from_other_scope_never_authorizes_all(mention_env, mismatch):
    db, channel, conversation = mention_env
    if mismatch == "channel":
        other = Channel(tenant_id=1, bot_id=channel.bot_id, channel_type="wechat_webridge", name="其它渠道")
        db.add(other)
        db.commit()
        room = _room(db, other, owner=OWN)
    else:
        room = _room(db, channel, "other@chatroom" if mismatch == "room" else conversation.external_id)
    member = _member(db, room, OWN, role="admin")
    if mismatch == "room-tenant":
        room.tenant_id = 2
    elif mismatch == "member-tenant":
        member.tenant_id = 2
    elif mismatch == "room-bot":
        room.bot_id = channel.bot_id + 1
    elif mismatch == "member-bot":
        member.bot_id = channel.bot_id + 1
    elif mismatch == "conversation-tenant":
        conversation.tenant_id = 2
    db.commit()
    assert not can_mention_all(db, channel, conversation)
    with pytest.raises(ValueError, match="只有群主或管理员才能 @所有人"):
        validate_members(db, channel, conversation, [ALL_ID])
