"""消息中心发送、引用、群 @ 与异步撤回的真实路由/队列回归。"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, Message, MessageAttachment, TaskJob, WorkWechatContact
from app.services import message_actions, outbound_files
from app.services.guga_agent.contract import JOB_TYPE as GUGA_AGENT_JOB_TYPE
from app.services.wechat_webridge import service
from test_message_center import seeded  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "wxid_bot"}


@pytest.fixture()
def bridge(seeded, tmp_path, monkeypatch):  # noqa: F811
    client, ids, cid, mids, csrf = seeded
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", tmp_path / "uploads")
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_webridge"
        channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
        db.get(Conversation, cid).channel_type = "wechat_webridge"
        for index in (0, 1):
            row = db.get(Message, mids[index])
            row.external_message_id = f"wx_{index}"
            row.external_status = "sent"
        db.commit()
    return client, ids, cid, mids, csrf


def _post(client, cid, csrf, **fields):
    return client.post("/messages/send", data={"conversation_id": cid, "csrf_token": csrf, **fields})


def test_quote_and_mentions_use_channel_ids_and_persist_local_quote(bridge):
    client, ids, cid, mids, csrf = bridge
    result = _post(client, cid, csrf, content="你好", reply_to_id=mids[0], mention_ids=json.dumps(["wxid_a"]))
    assert result.status_code == 200 and result.json()["ok"]
    with ids["factory"]() as db:
        message = db.get(Message, result.json()["message_id"])
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
        params = json.loads(job.payload_json)["command"]["params"]
        assert params == {"receiver": "room@chatroom", "content": "你好", "quote_message_id": "wx_0", "mention_user_list": ["wxid_a"]}
        assert json.loads(message.raw_request_json)["reply_to"]["text"] == "第1条消息"
    html = client.get(f"/messages/history?conversation_id={cid}&after_id={mids[-1]}").json()["html"]
    assert 'class="msg-quote"' in html and "第1条消息" in html


@pytest.mark.parametrize("kind,param,command", [("image", "image_path", "send_image"), ("voice", "source", "send_voice"), ("video", "source", "send_video_from_url"), ("emoji", "source", "send_emoji"), ("file", "file_url", "send_file_from_url")])
def test_uploads_reach_the_correct_native_command_and_can_be_viewed(bridge, kind, param, command):
    client, ids, cid, mids, csrf = bridge
    result = client.post("/messages/send", data={"conversation_id": cid, "csrf_token": csrf, "message_type": kind}, files={"file": ("测试.gif", b"GIF89a-test", "image/gif")})
    assert result.status_code == 200 and result.json()["ok"]
    with ids["factory"]() as db:
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
        data = json.loads(job.payload_json)["command"]
        assert data["type"] == command and "/outbound-files/" in data["params"][param]
        attachment = db.scalars(select(MessageAttachment).where(MessageAttachment.message_id == result.json()["message_id"])).one()
        attachment_id = int(attachment.id)
    assert client.get(f"/media/attachments/{attachment_id}").content == b"GIF89a-test"
    html = client.get(f"/messages/history?conversation_id={cid}&after_id={mids[-1]}").json()["html"]
    assert f'data-media-type="{kind}"' in html and 'data-state="ready"' in html


def test_unsupported_media_and_invalid_references_never_enqueue(bridge):
    client, ids, cid, mids, csrf = bridge
    with ids["factory"]() as db:
        db.get(Channel, ids["channel_id"]).runtime_info_json = json.dumps({"webridge": {**IDENTITY, "capabilities": ["send_message"]}})
        db.commit()
    assert "不支持" in _post(client, cid, csrf, message_type="video").json()["notice"]
    assert "不属于" in _post(client, cid, csrf, content="hi", reply_to_id=999999).json()["notice"]
    assert "不属于" in _post(client, cid, csrf, content="hi", mention_ids='["outsider"]').json()["notice"]
    with ids["factory"]() as db:
        assert not db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all()


def test_revoke_waits_for_phone_confirmation_and_sync_updates_the_existing_row(bridge):
    client, ids, cid, mids, csrf = bridge
    result = client.post("/messages/revoke", data={"conversation_id": cid, "message_id": mids[1], "csrf_token": csrf})
    assert result.status_code == 200 and result.json()["ok"]
    with ids["factory"]() as db:
        message = db.get(Message, mids[1])
        assert message.status == "sent" and json.loads(message.raw_request_json)["revoke"]["state"] == "pending"
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
        assert json.loads(job.payload_json)["command"]["params"]["message_id"] == "wx_1"
        job.status = "running"
        service.settle_outbound_message(db, job, ok=True)
        db.commit()
        assert message.status == "revoked"
    synced = client.get(f"/messages/history?conversation_id={cid}&after_id={mids[-1]}&sync_ids={mids[1]}").json()
    assert synced["html"] == "" and "你撤回了一条消息" in synced["updates_html"]
    assert 'data-revoke-state="revoked"' in synced["updates_html"]


def test_revoke_rejects_other_people_expired_and_unconfirmed_messages(bridge):
    client, ids, cid, mids, csrf = bridge
    url = "/messages/revoke"
    def revoke(mid):
        return client.post(url, data={"conversation_id": cid, "message_id": mid, "csrf_token": csrf})
    assert "自己" in revoke(mids[0]).json()["notice"]
    with ids["factory"]() as db:
        db.get(Message, mids[1]).created_at = now_shanghai() - timedelta(seconds=121)
        db.commit()
    assert "2 分钟" in revoke(mids[1]).json()["notice"]
    assert revoke(mids[3]).status_code == 400  # 缺少渠道消息 ID
    assert client.post(url, data={"conversation_id": cid, "message_id": mids[1]}).status_code == 403
    _login(client, "readonly")
    assert revoke(mids[1]).status_code == 403


def test_revoke_failure_keeps_content_and_allows_retry(bridge):
    client, ids, cid, mids, csrf = bridge
    data = {"conversation_id": cid, "message_id": mids[1], "csrf_token": csrf}
    assert client.post("/messages/revoke", data=data).json()["ok"]
    with ids["factory"]() as db:
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
        service.settle_outbound_message(db, job, ok=False, error="手机离线")
        db.commit()
        message = db.get(Message, mids[1])
        assert message.status == "sent" and message.content == "第2条消息"
        assert json.loads(message.raw_request_json)["revoke"]["state"] == "failed"
    assert client.post("/messages/revoke", data=data).json()["ok"]


def test_phone_reports_native_revoke_failure_inside_successful_response(bridge):
    client, ids, cid, mids, csrf = bridge
    assert client.post("/messages/revoke", data={"conversation_id": cid, "message_id": mids[1], "csrf_token": csrf}).json()["ok"]
    with ids["factory"]() as db:
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
        job.status = "running"
        db.commit()
        request_id = json.loads(job.payload_json)["command"]["request_id"]
        service.accept_command_result(db, db.get(Channel, ids["channel_id"]), {**IDENTITY, "request_id": request_id, "ok": True, "result": {"result": {"ok": False, "error": "原消息无法撤回"}}})
        db.commit()
        message = db.get(Message, mids[1])
        assert message.status == "sent"
        assert json.loads(message.raw_request_json)["revoke"]["state"] == "failed"


def test_sender_identity_and_capabilities_are_resolved_from_channel_data(bridge):
    client, ids, cid, mids, _csrf = bridge
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        message = db.get(Message, mids[0])
        message.sender_id = "wxid_bot"
        assert message_actions.is_own_message(message, channel)
        from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
        normalized = WechatWeBridgeAdapter().normalize_inbound_payload(channel, {"body": {"sender_id": "wxid_bot", "chat_id": "room@chatroom", "content": "本机发出"}})
        assert normalized["details"]["direction"] == "out"
        db.commit()
    data = client.get(f"/messages/capabilities?conversation_id={cid}").json()
    assert data["ok"] and all(data["capabilities"].values())
    html = client.get(f"/messages/history?conversation_id={cid}&before_id={mids[29]}").json()["html"]
    assert re.search(rf'class="wechat-message-row out" data-id="{mids[0]}" data-own="1"', html)


def test_outbound_result_saves_the_native_message_id_for_quote_and_revoke(bridge):
    client, ids, cid, _mids, csrf = bridge
    result = _post(client, cid, csrf, content="后台消息").json()
    with ids["factory"]() as db:
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
        service.settle_outbound_message(db, job, ok=True, result={"ok": True, "result": {"message_id": "wx-real-id"}})
        db.commit()
        message = db.get(Message, result["message_id"])
        assert message.external_message_id == "wx-real-id"
        assert message_actions.revoke_info(message, db.get(Channel, ids["channel_id"]))["revoke_until"] > 0


def test_expired_revoke_is_not_delivered_after_waiting_in_queue(bridge):
    client, ids, cid, mids, csrf = bridge
    assert client.post("/messages/revoke", data={"conversation_id": cid, "message_id": mids[1], "csrf_token": csrf}).json()["ok"]
    with ids["factory"]() as db:
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
        payload = json.loads(job.payload_json)
        payload["revoke_expires_at"] = int(now_shanghai().timestamp() * 1000) - 1
        job.payload_json = json.dumps(payload)
        db.commit()
        with patch.object(service, "_poll_maintenance"):
            assert service.claim_commands(db, db.get(Channel, ids["channel_id"]), IDENTITY) == []
        assert job.status == "failed"
        message = db.get(Message, mids[1])
        assert message.status == "sent" and message_actions.revoke_info(message, db.get(Channel, ids["channel_id"]))["revoke_state"] == "failed"


def test_conversation_poll_finds_new_conversations_and_keeps_filters(bridge):
    client, ids, cid, mids, csrf = bridge
    before = client.get("/messages/conversations").json()
    assert before["ok"] and [row["id"] for row in before["conversations"]] == [cid]
    with ids["factory"]() as db:
        direct = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], channel_type="wechat_webridge", external_id="new-contact", chat_type="direct", title="新联系人", last_message_at=now_shanghai())
        db.add(direct)
        db.flush()
        db.add(Message(tenant_id=ids["alpha"], conversation_id=direct.id, direction="in", content="新消息", message_type="text"))
        db.commit()
        new_id = int(direct.id)
    all_rows = client.get("/messages/conversations").json()["conversations"]
    assert all_rows[0]["id"] == new_id and all_rows[0]["preview"] == "新消息"
    filtered = client.get(f"/messages/conversations?channel_id={ids['channel_id']}&chat_type=group").json()["conversations"]
    assert [row["id"] for row in filtered] == [cid]
    assert client.get("/messages/conversations?bot_id=999999").json()["conversations"] == []


def test_linked_historical_echo_is_hidden_and_removed_from_open_threads(bridge):
    client, ids, cid, mids, _csrf = bridge
    with ids["factory"]() as db:
        duplicate = db.get(Message, mids[-1])
        duplicate.status = "echo"
        duplicate.raw_request_json = json.dumps({"echo_of": mids[-2]})
        db.commit()
    response = client.get(f"/messages/history?conversation_id={cid}&after_id={mids[-3]}&sync_ids={mids[-2]},{mids[-1]}").json()
    assert f'data-id="{mids[-1]}"' not in response["html"]
    assert f'data-id="{mids[-1]}"' not in response["updates_html"]
    assert response["removed_ids"] == [mids[-1]]
    assert f'data-id="{mids[-2]}"' in response["html"]
    latest = client.get("/messages/conversations").json()["conversations"][0]
    assert latest["preview"] == "第74条消息"
    page = client.get(f"/messages?conversation_id={cid}").text
    assert f'data-id="{mids[-1]}"' not in page
    assert "消息 74" in page


def test_gugatool_quote_send_uses_native_ids_from_message_snapshot(seeded):  # noqa: F811
    """咕嘎引用发送只认本地/服务端编号:入站时存下的快照要能直接用于 send_quote。"""

    client, ids, cid, mids, csrf = seeded
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_gugatool"
        channel.runtime_info_json = json.dumps({"guga_agent": {"capabilities": ["send_text", "send_quote"]}})
        db.get(Conversation, cid).channel_type = "wechat_gugatool"
        reply = db.get(Message, mids[0])
        reply.external_message_id = "evt-quote"
        reply.raw_request_json = json.dumps({"native_ids": {"local": 1097, "server": 2773629253319918141}})
        db.commit()

    result = _post(client, cid, csrf, content="收到", reply_to_id=mids[0])

    assert result.status_code == 200 and result.json()["ok"]
    with ids["factory"]() as db:
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == GUGA_AGENT_JOB_TYPE)).one()
        command = json.loads(job.payload_json)["command"]
        assert command["command_name"] == "send_quote"
        assert command["params"] == {
            "to_wxid": "room@chatroom",
            "content": "收到",
            "local_id": 1097,
            "msg_svr_id": 2773629253319918141,
        }


def test_gugatool_quote_without_native_ids_is_rejected(seeded, monkeypatch):  # noqa: F811
    """只有事件 UUID 时不能硬发引用,要给出可读提示而不是让手机整条拒收。"""

    client, ids, cid, mids, csrf = seeded
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_gugatool"
        channel.runtime_info_json = json.dumps({"guga_agent": {"capabilities": ["send_text", "send_quote"]}})
        db.get(Conversation, cid).channel_type = "wechat_gugatool"
        reply = db.get(Message, mids[0])
        reply.external_message_id = "evt-no-native"
        db.commit()
    monkeypatch.setattr(message_actions, "guga_quote_ids", lambda *args, **kwargs: {})

    result = _post(client, cid, csrf, content="收到", reply_to_id=mids[0])

    assert result.status_code == 400
    assert "编号" in result.json()["notice"]
    with ids["factory"]() as db:
        assert db.scalars(select(TaskJob).where(TaskJob.job_type == GUGA_AGENT_JOB_TYPE)).all() == []


def test_gugatool_outbound_quote_falls_back_to_command_receipt(seeded):  # noqa: F811
    """机器人刚发出去的消息没有事件快照时,用手机命令回执里的 LocalId 也能引用。"""

    client, ids, cid, mids, csrf = seeded
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_gugatool"
        message = db.get(Message, mids[1])
        message.direction = "out"
        message.status = "sent"
        message.external_status = "sent"
        message.external_message_id = None
        message.raw_request_json = json.dumps({"request_id": "req-quote"})
        db.add(TaskJob(tenant_id=ids["alpha"], job_type=GUGA_AGENT_JOB_TYPE, status="completed", priority=90, payload_json="{}", dedupe_key=f"guga:{channel.id}:req-quote", result_json=json.dumps({"Data": {"LocalId": 33300, "MsgSvrId": 0}})))
        db.commit()

        assert message_actions.quote_payload_for(db, channel, message) == {"quote_message_id": "33300", "quote_local_id": 33300}


def test_gugatool_revoke_uses_phone_revoke_command_and_settles(seeded):  # noqa: F811
    """咕嘎撤回走 revoke_message:ToWxid + LocalId/MsgSvrId,手机确认后才把原消息标成已撤回。"""

    client, ids, cid, mids, csrf = seeded
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_gugatool"
        channel.runtime_info_json = json.dumps({"guga_agent": {"capabilities": ["send_text", "revoke_message"]}})
        db.get(Conversation, cid).channel_type = "wechat_gugatool"
        message = db.get(Message, mids[1])
        message.direction = "out"
        message.status = "sent"
        message.external_status = "sent"
        message.external_message_id = "33300"
        message.created_at = now_shanghai()
        message.raw_request_json = json.dumps({"native_ids": {"local": 33300}})
        db.commit()
        message_id = int(message.id)

    result = client.post("/messages/revoke", data={"conversation_id": cid, "message_id": message_id, "csrf_token": csrf})

    assert result.status_code == 200 and result.json()["ok"]
    with ids["factory"]() as db:
        job = db.scalars(select(TaskJob).where(TaskJob.job_type == GUGA_AGENT_JOB_TYPE).order_by(TaskJob.id.desc())).first()
        data = json.loads(job.payload_json)
        assert data["command"]["command_name"] == "revoke_message"
        assert data["command"]["params"] == {"ToWxid": "room@chatroom", "LocalId": 33300}
        assert data["revoke_message_id"] == message_id
        message_actions.settle_revoke(db, job, ok=True)
        db.commit()
        assert db.get(Message, message_id).status == "revoked"


def test_gugatool_self_revoke_notice_updates_original_without_fake_peer_message(seeded):  # noqa: F811
    """自己撤回后手机会回一条 10002 通知:只更新原消息,不再生成“对方撤回了一条消息”。"""

    from app.core.tenant_context import tenant_scope
    from app.core.tenant_db import open_tenant_session
    from app.services import channel_events

    client, ids, cid, mids, csrf = seeded
    with tenant_scope(ids["alpha"]), open_tenant_session(ids["factory"]) as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_gugatool"
        conversation = db.get(Conversation, cid)
        conversation.channel_type = "wechat_gugatool"
        original = db.get(Message, mids[1])
        original.direction = "out"
        original.content = "123"
        original.status = "sent"
        original.external_status = "sent"
        original.external_message_id = "1106"
        # 原消息必须有明确微信编号快照，正文相同不能作为撤回证据。
        original.raw_request_json = json.dumps({"native_ids": {"server": 1106}})
        original.created_at = now_shanghai()
        db.commit()
        before = db.scalar(select(Message.id).where(Message.conversation_id == cid).order_by(Message.id.desc()))
        event = {
            "id": "evt-recall",
            "kind": "incoming",
            "fromUser": conversation.external_id,
            "toUser": "wxid_self",
            "messageType": 10002,
            "content": (
                '<sysmsg type="revokemsgcontent"><revokemsgcontent><link><scene>revokemsgcontent</scene>'
                "<reEditStringContent>123</reEditStringContent><stringContent>你撤回了一条消息</stringContent></link>"
                "<reEditReferMsgSvrId>1106</reEditReferMsgSvrId>"
                "<text>你撤回了一条消息</text></revokemsgcontent></sysmsg>"
            ),
        }
        result = channel_events.ingest_inbound_event(db, channel, event)
        db.commit()

        assert result["dispatch"]["reason"] == "message_recalled"
        assert result["dispatch"]["matched"] is True
        assert db.get(Message, int(original.id)).status == "revoked"
        # 没有新增一条署名为对方的“对方撤回了一条消息”。
        assert db.scalar(select(Message.id).where(Message.conversation_id == cid).order_by(Message.id.desc())) == before


def test_start_chat_from_friend_creates_and_reuses_direct_conversation(seeded):  # noqa: F811
    """好友列表没有会话时可以直接发起聊天;重复点击复用同一条私聊会话。"""

    client, ids, cid, mids, csrf = seeded
    with ids["factory"]() as db:
        # 发起聊天目前只对咕嘎渠道开放;夹具渠道先切到 gugatool。
        db.get(Channel, ids["channel_id"]).channel_type = "wechat_gugatool"
        contact = WorkWechatContact(
            tenant_id=ids["alpha"],
            channel_id=ids["channel_id"],
            contact_kind="friend",
            contact_external_id="wxid_new_friend",
            display_name="新好友",
            sync_status="active",
        )
        db.add(contact)
        db.commit()
        contact_id = int(contact.id)

    page = client.get(f"/contacts?channel_id={ids['channel_id']}").text
    assert "发起聊天" in page

    first = client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf}, follow_redirects=False)
    assert first.status_code == 303
    assert "/messages?conversation_id=" in first.headers["location"]
    with ids["factory"]() as db:
        conversation = db.scalar(select(Conversation).where(Conversation.external_id == "wxid_new_friend"))
        assert conversation is not None and conversation.chat_type == "direct" and conversation.title == "新好友"
        conversation_id = int(conversation.id)

    second = client.post(f"/contacts/{contact_id}/chat", data={"csrf_token": csrf}, follow_redirects=False)
    assert second.status_code == 303
    assert f"conversation_id={conversation_id}" in second.headers["location"]
