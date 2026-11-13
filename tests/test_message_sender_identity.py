"""消息在网页入库与手机回显前后，必须使用同一份本账号头像昵称。"""

from __future__ import annotations

import json

import pytest

from app.models.entities import Bot, Channel, Conversation, Message, WorkWechatContact
from app.models.saas import ProviderNode, ProviderNodeAccount, Tenant
from app.services.identity_directory import IdentityDirectory
from tests.test_ai_chat import _session
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


OWN = "wxid_identity_bot"
AVATAR = "https://avatars.test/own.jpg"


@pytest.fixture()
def identity_env():
    db = _session()
    db.add(Tenant(id=1, code="identity", name="身份测试"))
    bot = Bot(tenant_id=1, name="测试机器人")
    db.add(bot)
    db.flush()
    channel = Channel(tenant_id=1, bot_id=bot.id, channel_type="wechat_webridge", name="自己的账号",
                      runtime_info_json=json.dumps({"webridge": {"account_id": OWN},
                                                    "account_connection": {"account_id": OWN, "display_name": "自己昵称"},
                                                    "account_profile": {"account_id": OWN, "avatar_url": AVATAR}}))
    db.add(channel)
    db.flush()
    conversation = Conversation(tenant_id=1, bot_id=bot.id, channel_id=channel.id,
                                channel_type=channel.channel_type, external_id="wxid_friend", chat_type="direct", title="对方昵称")
    db.add(conversation)
    db.commit()
    yield db, channel, conversation
    db.close()


def _message(db, conversation, *, sender=None, name="", direction="out"):
    message = Message(tenant_id=1, conversation_id=conversation.id, direction=direction, sender_id=sender,
                      sender_name=name, content="消息正文", message_type="text")
    db.add(message)
    db.commit()
    return message


def _contact(db, channel, external_id, name, avatar):
    db.add(WorkWechatContact(tenant_id=1, channel_id=channel.id, contact_external_id=external_id,
                            display_name=name, avatar_url=avatar))
    db.commit()


@pytest.mark.parametrize("sender,direction", [(None, "out"), ("outbound-adapter:wechat_webridge:1", "out"),
                                              ("admin-console", "out"), (OWN, "out"), (OWN, "in")])
def test_own_messages_always_use_channel_account_identity(identity_env, sender, direction):
    db, channel, conversation = identity_env
    _contact(db, channel, OWN, "目录旧昵称", "https://avatars.test/old.jpg")
    _contact(db, channel, "wxid_friend", "对方昵称", "https://avatars.test/friend.jpg")
    message = _message(db, conversation, sender=sender, direction=direction, name="回显临时昵称")
    identity = IdentityDirectory(db).for_messages([message])[message.id]
    assert identity["is_own"] and not identity["show_id"]
    assert (identity["name"], identity["avatar_url"], identity["initial"]) == ("自己昵称", AVATAR, "自")


def test_phone_echo_does_not_change_existing_card_identity(identity_env):
    db, _channel, conversation = identity_env
    message = _message(db, conversation, sender="outbound-adapter:wechat_webridge:1")
    before = IdentityDirectory(db).for_messages([message])[message.id]
    message.sender_id, message.sender_name = OWN, "另一份临时昵称"
    db.commit()
    after = IdentityDirectory(db).for_messages([message])[message.id]
    assert before == after


def test_message_history_renders_same_account_avatar_before_and_after_echo(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_webridge"
        channel.runtime_info_json = json.dumps({"webridge": {"account_id": OWN},
                                                "account_connection": {"account_id": OWN, "display_name": "自己昵称"},
                                                "account_profile": {"avatar_url": AVATAR}})
        conversation = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id,
                                    channel_type=channel.channel_type, external_id="friend", chat_type="direct")
        db.add(conversation)
        db.flush()
        message = Message(tenant_id=ids["alpha"], conversation_id=conversation.id, direction="out",
                          sender_id="outbound-adapter:wechat_webridge:1", content="网页发送测试", message_type="text")
        db.add(message)
        db.commit()
        cid, mid = int(conversation.id), int(message.id)
    _login(client, "admin")
    before = client.get(f"/messages/history?conversation_id={cid}").json()["html"]
    with ids["factory"]() as db:
        message = db.get(Message, mid)
        message.sender_id, message.sender_name = OWN, "手机临时昵称"
        db.commit()
    after = client.get(f"/messages/history?conversation_id={cid}&sync_ids={mid}").json()["html"]
    for html in (before, after):
        assert 'class="wechat-message-row out"' in html and 'data-own="1"' in html
        assert 'data-sender-name="自己昵称"' in html and 'aria-label="自己昵称的头像"' in html
        assert f'<img src="{AVATAR}"' in html
        assert "手机临时昵称" not in html


def test_own_directory_avatar_is_used_before_echo_if_profile_has_none(identity_env):
    db, channel, conversation = identity_env
    channel.runtime_info_json = json.dumps({"webridge": {"account_id": OWN}})
    _contact(db, channel, OWN, "账号目录昵称", AVATAR)
    before = _message(db, conversation, sender="outbound-adapter:wechat_webridge:1")
    after = _message(db, conversation, sender=OWN, name="手机昵称")
    identities = IdentityDirectory(db).for_messages([before, after])
    assert identities[before.id] == identities[after.id]
    assert identities[before.id]["avatar_url"] == AVATAR


def test_missing_own_avatar_uses_stable_fallback_without_other_person_data(identity_env):
    db, channel, conversation = identity_env
    channel.name = OWN
    channel.runtime_info_json = json.dumps({"webridge": {"account_id": OWN}})
    _contact(db, channel, "wxid_friend", "对方昵称", "https://avatars.test/friend.jpg")
    before = _message(db, conversation, sender=None)
    after = _message(db, conversation, sender=OWN, name="消息临时昵称")
    identities = IdentityDirectory(db).for_messages([before, after])
    assert identities[before.id] == identities[after.id]
    identity = identities[before.id]
    assert (identity["name"], identity["initial"], identity["avatar_url"]) == ("我方", "我", "")
    assert not identity["show_id"]


def test_unconfirmed_sender_cannot_supply_own_avatar(identity_env):
    db, channel, conversation = identity_env
    channel.runtime_info_json = "{}"
    _contact(db, channel, "wxid_friend", "对方昵称", "https://avatars.test/friend.jpg")
    before = _message(db, conversation, sender="outbound-adapter:wechat_webridge:1")
    after = _message(db, conversation, sender="wxid_friend", name="对方昵称")
    identities = IdentityDirectory(db).for_messages([before, after])
    assert identities[before.id] == identities[after.id]
    assert identities[before.id]["avatar_url"] == ""
    assert identities[before.id]["name"] == "自己的账号"


@pytest.mark.parametrize("kind", ["wechat_webridge", "wechat_ipad", "workwechat", "telegram"])
def test_other_people_keep_directory_identity_and_incoming_direction(identity_env, kind):
    db, channel, conversation = identity_env
    channel.channel_type = conversation.channel_type = kind
    _contact(db, channel, "wxid_friend", "对方备注", "https://avatars.test/friend.jpg")
    message = _message(db, conversation, sender="wxid_friend", direction="in", name="消息昵称")
    identity = IdentityDirectory(db).for_messages([message])[message.id]
    assert not identity["is_own"]
    assert (identity["name"], identity["avatar_url"], identity["external_id"]) == (
        "对方备注", "https://avatars.test/friend.jpg", "wxid_friend")


def test_identity_cache_is_scoped_to_channel(identity_env):
    db, channel, conversation = identity_env
    other_channel = Channel(tenant_id=1, bot_id=channel.bot_id, channel_type="wechat_webridge", name="第二账号",
                            runtime_info_json=json.dumps({"webridge": {"account_id": OWN},
                                                          "account_profile": {"avatar_url": "https://avatars.test/second.jpg"}}))
    db.add(other_channel)
    db.flush()
    other = Conversation(tenant_id=1, bot_id=channel.bot_id, channel_id=other_channel.id,
                         channel_type="wechat_webridge", external_id="wxid_friend", chat_type="direct")
    db.add(other)
    db.commit()
    first = _message(db, conversation, sender=None)
    second = _message(db, other, sender=OWN)
    directory = IdentityDirectory(db)
    identities = directory.for_messages([first, second])
    assert identities[first.id]["avatar_url"] == AVATAR
    assert identities[second.id]["avatar_url"] == "https://avatars.test/second.jpg"
    assert directory.for_messages([first])[first.id] == identities[first.id]


@pytest.mark.parametrize("source", ["ipad-config", "provider-binding"])
def test_historical_own_messages_keep_existing_channel_identity_fallbacks(identity_env, source):
    db, channel, conversation = identity_env
    channel.runtime_info_json = "{}"
    if source == "ipad-config":
        channel.channel_type = conversation.channel_type = "wechat_ipad"
        channel.adapter_config_json = json.dumps({"wxid": OWN})
    else:
        channel.channel_type = conversation.channel_type = "workwechat"
        node = ProviderNode(tenant_id=1, name="身份节点")
        db.add(node)
        db.flush()
        account = ProviderNodeAccount(tenant_id=1, provider_node_id=node.id, account_key="self",
                                      external_user_id=OWN, display_name="绑定账号")
        db.add(account)
        db.flush()
        channel.provider_account_id = account.id
    _contact(db, channel, OWN, "自己的目录昵称", AVATAR)
    before = _message(db, conversation, sender="outbound-adapter:test:1")
    historical = _message(db, conversation, sender=OWN, direction="in")
    identities = IdentityDirectory(db).for_messages([before, historical])
    assert identities[before.id] == identities[historical.id]
    assert identities[historical.id]["is_own"] and identities[historical.id]["avatar_url"] == AVATAR
