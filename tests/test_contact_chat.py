"""好友发起私聊的跨渠道入口及真实认证边界；创建会话不会发送消息。"""

from dataclasses import replace

from sqlalchemy import func, select

from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Conversation, Message, TaskJob, WorkWechatContact
from app.services import channel_catalog
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


def _friend(db, ids, *, channel_id=None, external_id="new-friend", tenant_id=None, **values):
    contact = WorkWechatContact(tenant_id=tenant_id or ids["alpha"], channel_id=channel_id or ids["channel_id"],
        contact_kind="friend", contact_external_id=external_id, display_name="新好友", sync_status="active", **values)
    db.add(contact)
    db.flush()
    return contact


def test_all_current_channels_create_and_reuse_direct_chat(provider_page_context, monkeypatch):  # noqa: F811
    """遍历正式渠道，从好友列表真实点击；同名好友在其他账号的会话不能被复用。"""

    import app.main as main

    client, ids = provider_page_context
    csrf = _login(client, "operator")
    monkeypatch.setattr(main, "dispatch_outbound_message", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("创建会话不得发送消息")))
    for channel_type in channel_catalog.list_supported_channel_types():
        with ids["factory"]() as db:
            channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name=channel_type,
                channel_type=channel_type, is_enabled=True)
            db.add(channel)
            db.flush()
            contact = _friend(db, ids, channel_id=channel.id)
            channel_id, contact_id = channel.id, contact.id
            db.commit()
        page = client.get(f"/contacts?channel_id={channel_id}")
        assert page.status_code == 200 and f'action="/contacts/{contact_id}/chat"' in page.text
        first = client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf})
        assert first.status_code == 303 and "/messages?conversation_id=" in first.headers["location"]
        second = client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf})
        assert second.headers["location"] == first.headers["location"]
        with ids["factory"]() as db:
            conversations = db.scalars(select(Conversation).where(Conversation.channel_id == channel_id)).all()
            assert len(conversations) == 1
            conversation = conversations[0]
            assert (conversation.tenant_id, conversation.bot_id, conversation.chat_type, conversation.title) == (ids["alpha"], ids["bot"], "direct", "新好友")
            assert db.scalar(select(func.count()).select_from(Message)) == 0
            assert db.scalar(select(func.count()).select_from(TaskJob)) == 0
        assert "聊天记录" in client.get(f"/contacts?channel_id={channel_id}").text


def test_contact_chat_enforces_tenant_role_csrf_and_lifecycle(provider_page_context):  # noqa: F811
    """无效好友、删除/停用账号、跨租户记录与只读角色不能写入会话。"""

    client, ids = provider_page_context
    csrf = _login(client, "operator")
    with ids["factory"]() as db:
        contact = _friend(db, ids)
        foreign_bot = Bot(tenant_id=ids["beta"], name="其他企业机器人")
        db.add(foreign_bot)
        db.flush()
        foreign_channel = Channel(tenant_id=ids["beta"], bot_id=foreign_bot.id, name="其他企业",
            channel_type="wechat_gugatool", is_enabled=True)
        db.add(foreign_channel)
        db.flush()
        foreign = _friend(db, ids, channel_id=foreign_channel.id, tenant_id=ids["beta"])
        contact_id, foreign_id = contact.id, foreign.id
        db.commit()
    assert client.post(f"/contacts/{foreign_id}/chat", data={"csrf_token": csrf}).status_code == 404
    assert client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": "wrong"}).status_code == 403
    for field, value in (("sync_status", "deleted"), ("deleted_at", now_shanghai()), ("contact_kind", "official_account")):
        with ids["factory"]() as db:
            row = db.get(WorkWechatContact, contact_id)
            previous = getattr(row, field)
            setattr(row, field, value)
            db.commit()
        assert client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf}).status_code == 404
        assert f'action="/contacts/{contact_id}/chat"' not in client.get("/contacts").text
        with ids["factory"]() as db:
            setattr(db.get(WorkWechatContact, contact_id), field, previous)
            db.commit()
    for model, field, value in ((Channel, "deleted_at", now_shanghai()), (Channel, "is_enabled", False), (Bot, "deleted_at", now_shanghai())):
        with ids["factory"]() as db:
            row = db.get(model, ids["channel_id"] if model is Channel else ids["bot"])
            previous = getattr(row, field)
            setattr(row, field, value)
            db.commit()
        result = client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf})
        assert result.status_code in (400, 404)
        assert f'action="/contacts/{contact_id}/chat"' not in client.get("/contacts").text
        with ids["factory"]() as db:
            setattr(db.get(model, ids["channel_id"] if model is Channel else ids["bot"]), field, previous)
            db.commit()
    readonly_csrf = _login(client, "readonly")
    assert client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": readonly_csrf}).status_code == 403
    assert f'action="/contacts/{contact_id}/chat"' not in client.get("/contacts").text
    with ids["factory"]() as db:
        assert db.scalar(select(func.count()).select_from(Conversation)) == 0


def test_direct_chat_uses_catalog_capability_and_never_reuses_a_group(provider_page_context, monkeypatch):  # noqa: F811
    """页面与写入口都服从能力声明；相同外部 ID 的群会话不能当作好友私聊。"""

    client, ids = provider_page_context
    csrf = _login(client, "operator")
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_gugatool"
        contact = _friend(db, ids)
        group = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id,
            channel_type=channel.channel_type, external_id=contact.contact_external_id, chat_type="group")
        db.add(group)
        db.flush()
        contact_id, group_id = contact.id, group.id
        db.commit()
    original = channel_catalog.get_channel_type_definition("wechat_gugatool")
    monkeypatch.setitem(channel_catalog._CHANNEL_TYPE_BY_CODE, original.code,
        replace(original, capabilities=original.capabilities - {"direct_chat"}))
    assert f'action="/contacts/{contact_id}/chat"' not in client.get("/contacts").text
    assert client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf}).status_code == 400
    monkeypatch.setitem(channel_catalog._CHANNEL_TYPE_BY_CODE, original.code, original)
    response = client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf})
    assert response.status_code == 303 and f"conversation_id={group_id}&" not in response.headers["location"]
