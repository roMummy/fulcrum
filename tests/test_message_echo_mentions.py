"""按已知成员昵称认领原生 @ 回显，正文和实际发送次数必须保持不变。"""

from __future__ import annotations

import json
from html import escape

import pytest
from sqlalchemy import select

from app.models.entities import Channel, Chatroom, ChatroomMember, WorkWechatContact
from tests.test_webridge_message_echo import OWN, _event, _reconcile, _send, echo_env


FIRST = "wxid_mention_first"
SECOND = "wxid_mention_second"


def _member(db, channel, conversation, *, member_id=FIRST, nickname="九命", group_name="网页群昵称", room_id=None):
    external_id = room_id or conversation.external_id
    room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == external_id))
    if room is None:
        room = Chatroom(tenant_id=channel.tenant_id, bot_id=channel.bot_id, channel_id=channel.id,
                        external_id=external_id, name="测试群")
        db.add(room)
        db.flush()
    member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id,
                                                   ChatroomMember.member_external_id == member_id))
    if member is None:
        member = ChatroomMember(tenant_id=channel.tenant_id, bot_id=channel.bot_id, channel_id=channel.id,
                                chatroom_id=room.id, member_external_id=member_id)
        db.add(member)
    member.display_name, member.nickname, member.status = group_name, nickname, "active"
    db.commit()


def _mentioned_event(channel, conversation, content, *, members=None, native_id="mention-native-1"):
    event = _event(channel, conversation, content=content, native_id=native_id)
    event["details"]["mention_user_list"] = members if members is not None else [FIRST]
    return event


@pytest.mark.parametrize("nickname", ["九命", "水牛@rCodex", "手机 名字", "手机\t名字", "手机\n名字", "姓名\u2005间隔"],
                         ids=["contact-nickname", "literal-at", "space", "tab", "newline", "native-space"])
def test_native_inline_mentions_do_not_require_a_separator(echo_env, nickname):
    db, channel, conversation = echo_env
    _member(db, channel, conversation, nickname=nickname)
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}测试@水牛@rCodex",
                         display_content="@网页群昵称 测试@水牛@rCodex", mention_user_list=[FIRST])
    result = _reconcile(db, channel, conversation,
                        _mentioned_event(channel, conversation, "@" + nickname + "测试@水牛@rCodex"))
    assert result is not None and result.id == original.id
    assert original.content == "@网页群昵称 测试@水牛@rCodex"


@pytest.mark.parametrize("field", ["display_name", "name"], ids=["friend-remark", "contact-name"])
def test_native_mention_can_use_same_channel_contact_name(echo_env, field):
    db, channel, conversation = echo_env
    _member(db, channel, conversation, nickname="目录旧昵称")
    db.add(WorkWechatContact(tenant_id=channel.tenant_id, channel_id=channel.id, contact_external_id=FIRST,
                             **{field: "手机备注@名字"}))
    db.commit()
    original, _ = _send(db, channel, conversation, content="前{{mention:" + FIRST + "}}后", mention_user_list=[FIRST])
    result = _reconcile(db, channel, conversation, _mentioned_event(channel, conversation, "前@手机备注@名字后"))
    assert result is not None and result.id == original.id


def test_multiple_and_repeated_inline_mentions_preserve_literal_body(echo_env):
    db, channel, conversation = echo_env
    _member(db, channel, conversation)
    _member(db, channel, conversation, member_id=SECOND, nickname="水牛@rCodex")
    content = "前{{mention:" + FIRST + "}}测试{{mention:" + SECOND + "}}\n正文@手写，再{{mention:" + FIRST + "}}。"
    original, _ = _send(db, channel, conversation, content=content, mention_user_list=[FIRST, SECOND])
    event = _mentioned_event(channel, conversation, "前@九命测试@水牛@rCodex\n正文@手写，再@九命。", members=[SECOND, FIRST])
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id


def test_inline_mention_does_not_invent_prefix_for_unplaced_notification(echo_env):
    db, channel, conversation = echo_env
    _member(db, channel, conversation)
    _member(db, channel, conversation, member_id=SECOND, nickname="另一人")
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}正文",
                         mention_user_list=[FIRST, SECOND])
    result = _reconcile(db, channel, conversation,
                        _mentioned_event(channel, conversation, "@九命正文", members=[FIRST, SECOND]))
    assert result is not None and result.id == original.id


@pytest.mark.parametrize("content,members", [
    ("@九命测试水牛@rCodex", [FIRST]),
    ("@九命测试@水牛rCodex", [FIRST]),
    ("@九命 测试@水牛@rCodex", [FIRST]),
    ("@九命\t测试@水牛@rCodex", [FIRST]),
    ("@九命\u2005测试@水牛@rCodex", [FIRST]),
    ("@九命\n测试@水牛@rCodex", [FIRST]),
    ("@九命不要测试@水牛@rCodex", [FIRST]),
    ("@九命@额外的人 测试@水牛@rCodex", [FIRST]),
    ("@九命测试@水牛@rCodex", [SECOND]),
], ids=["removed-at", "changed-literal-at", "inserted-space", "inserted-tab", "inserted-native-space",
        "inserted-newline", "extra-body", "extra-prefix", "wrong-member"])
def test_known_nickname_does_not_hide_body_or_member_changes(echo_env, content, members):
    db, channel, conversation = echo_env
    _member(db, channel, conversation)
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}测试@水牛@rCodex",
                         mention_user_list=[FIRST])
    assert _reconcile(db, channel, conversation,
                      _mentioned_event(channel, conversation, content, members=members)) is None
    assert original.external_message_id is None


@pytest.mark.parametrize("source", ["other-group", "other-channel"])
def test_mention_names_are_scoped_to_current_group_and_channel(echo_env, source):
    db, channel, conversation = echo_env
    _member(db, channel, conversation)
    if source == "other-group":
        _member(db, channel, conversation, nickname="范围外昵称", room_id="another@chatroom")
    else:
        other = Channel(tenant_id=channel.tenant_id, bot_id=channel.bot_id,
                        channel_type="wechat_webridge", name="另一渠道")
        db.add(other)
        db.flush()
        db.add(WorkWechatContact(tenant_id=channel.tenant_id, channel_id=other.id,
                                 contact_external_id=FIRST, display_name="范围外昵称"))
        db.commit()
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}正文", mention_user_list=[FIRST])
    assert _reconcile(db, channel, conversation,
                      _mentioned_event(channel, conversation, "@范围外昵称正文")) is None
    assert original.external_message_id is None


def test_mention_can_use_saved_selection_when_directory_is_missing(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}正文", mention_user_list=[FIRST])
    raw = json.loads(original.raw_request_json)
    raw["mentions"] = [{"id": FIRST, "name": "选中@昵称"}]
    original.raw_request_json = json.dumps(raw)
    db.commit()
    result = _reconcile(db, channel, conversation, _mentioned_event(channel, conversation, "@选中@昵称正文"))
    assert result is not None and result.id == original.id


def test_phone_manual_repeat_after_claimed_mention_is_preserved(echo_env):
    db, channel, conversation = echo_env
    _member(db, channel, conversation)
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}正文", mention_user_list=[FIRST])
    first = _mentioned_event(channel, conversation, "@九命正文")
    result = _reconcile(db, channel, conversation, first)
    assert result is not None and result.id == original.id
    manual = _mentioned_event(channel, conversation, "@九命正文", native_id="phone-manual-repeat")
    assert _reconcile(db, channel, conversation, manual) is None


def test_two_identical_web_mentions_keep_two_messages(echo_env):
    db, channel, conversation = echo_env
    _member(db, channel, conversation)
    originals = [_send(db, channel, conversation, content="{{mention:" + FIRST + "}}正文", mention_user_list=[FIRST])[0]
                 for _ in range(2)]
    matches = [_reconcile(db, channel, conversation, _mentioned_event(channel, conversation, "@九命正文", native_id=native))
               for native in ("first-native", "second-native")]
    assert all(message is not None for message in matches)
    assert {message.id for message in matches} == {message.id for message in originals}
    assert _reconcile(db, channel, conversation, _mentioned_event(channel, conversation, "@九命正文", native_id="manual-native")) is None


@pytest.mark.parametrize("separator", [" ", "\t", "\u2005"])
def test_existing_inline_separator_is_preserved(echo_env, separator):
    db, channel, conversation = echo_env
    _member(db, channel, conversation)
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}" + separator + "正文", mention_user_list=[FIRST])
    result = _reconcile(db, channel, conversation, _mentioned_event(channel, conversation, "@九命" + separator + "正文"))
    assert result is not None and result.id == original.id


def test_quote_reply_uses_same_exact_native_mention_rendering(echo_env):
    db, channel, conversation = echo_env
    _member(db, channel, conversation, nickname="手机@名字")
    original, _ = _send(db, channel, conversation, content="{{mention:" + FIRST + "}}回复\n正文@原样",
                         mention_user_list=[FIRST], quote_message_id="quoted-original")
    title = escape("@手机@名字回复\n正文@原样")
    xml = (f"<msg><appmsg><title>{title}</title><type>57</type><refermsg>"
           "<svrid>quoted-original</svrid><content>原文</content></refermsg></appmsg></msg>")
    event = _event(channel, conversation, kind="app", content=OWN + ": " + xml)
    event["message"]["message_type"] = "quote"
    event["details"]["mention_user_list"] = [FIRST]
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id
