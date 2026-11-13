"""消息中心发送、引用、群 @ 与异步撤回的真实路由/队列回归。"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, Message, MessageAttachment, TaskJob
from app.services import message_actions, outbound_files
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
