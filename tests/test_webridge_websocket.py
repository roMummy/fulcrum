"""以真实 WebSocket 路由验证 WeBridge 8080 信封、账号确认和隔离。"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import select

from app.models.entities import Channel, RawWebhookEvent, TaskJob
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.services.wechat_webridge import service, ws
from app.services.wechat_webridge.access import connection_url, websocket_url
from test_webridge_connection_flow import (
    IDENTITY, _start, _confirm, _assert_unbound, bridge_context, isolate_bridge_runtime,
)
from test_provider_node_pages import provider_page_context


@pytest.fixture(autouse=True)
def websocket_runtime(bridge_context, monkeypatch):
    monkeypatch.setattr(ws, "TenantSessionLocal", bridge_context[1]["factory"])
    monkeypatch.setattr(ws, "COMMAND_CHECK_SECONDS", 0.02)


def _command(socket, name=None):
    for _ in range(12):
        frame = socket.receive_json()
        if frame["type"] == "ping":
            socket.send_json({"type": "pong", "ts_ms": 123})
        elif frame["type"] == "command" and (name is None or frame["command_name"] == name):
            return frame
    raise AssertionError("command not received")


def test_urls_and_mode_selector(bridge_context):
    client, ids, _ = bridge_context
    channel_id, address = _start(bridge_context)
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        assert websocket_url(channel) == connection_url(channel).replace("http://", "ws://") + "/ws"
        channel.api_base = channel.api_base.replace("http://", "https://")
        assert websocket_url(channel).startswith("wss://")
    page = client.get(f"/channels/connect?channel_id={channel_id}")
    assert 'data-webridge-transport' in page.text
    assert 'value="ws"' in page.text and address + "/ws" in page.text


def test_hello_commands_and_result_use_ws_contract(bridge_context):
    client, ids, _ = bridge_context
    channel_id, address = _start(bridge_context)
    with client.websocket_connect(address + "/ws") as socket:
        socket.send_json({**IDENTITY, "type": "hello", "capabilities": ["account_status", "send_message"]})
        assert socket.receive_json() == {"type": "hello_ack"}
        _assert_unbound(ids, channel_id)
        assert client.get(f"/channels/{channel_id}/connect/status").json()["connection"]["state"] == "verified"
        command = _command(socket, "account_status")
        assert set(command) == {"type", "request_id", "command_name", "args"}
        socket.send_json({**IDENTITY, "type": "command_result", "request_id": command["request_id"], "command_name": "account_status", "ok": True, "result": {"account_id": IDENTITY["account_id"], "nick_name": "测试 WS 昵称"}, "error": ""})
        # 同一接收任务按顺序处理，后续事件落库说明此前结果已经完成。
        assert _confirm(bridge_context, channel_id).status_code == 303
        with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
            job = service.enqueue_command(db, db.get(Channel, channel_id), command_name="send_message", args={"receiver": "wxid_test", "content": "测试"})
            db.commit()
            job_id = job.id
        sent = _command(socket, "send_message")
        assert sent["args"] == {"receiver": "wxid_test", "content": "测试"}
        socket.send_json({**IDENTITY, "type": "command_result", "request_id": sent["request_id"], "ok": False, "result": {"ok": True, "accepted": False, "diagnostic": "test failure"}, "error": "denied"})
        # 未知帧不回复；未知历史结果不应断开连接。
        socket.send_json({"type": "unknown"})
        socket.send_json({**IDENTITY, "type": "command_result", "request_id": "old-missing-result", "ok": True, "result": {}})
        with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
            follow = service.enqueue_command(db, db.get(Channel, channel_id), command_name="friends", args={})
            db.commit()
        _command(socket, "friends")
    with ids["factory"]() as db:
        assert db.get(TaskJob, job_id).status == "failed"
        result = json.loads(db.get(TaskJob, job_id).result_json)
        assert result["webridge"]["result"]["diagnostic"] == "test failure"
        assert json.loads(db.get(Channel, channel_id).runtime_info_json)["account_profile"]["nickname"] == "测试 WS 昵称"


@pytest.mark.parametrize("invalid", ["wrong", "crossed", "deleted"])
def test_bad_address_rejected_before_handshake(bridge_context, invalid):
    client, ids, _ = bridge_context
    channel_id, address = _start(bridge_context)
    if invalid == "crossed":
        other_id, _ = _start(bridge_context, "其他微桥")
        address = address.replace(f"/{channel_id}/", f"/{other_id}/")
    elif invalid == "deleted":
        from app.core.time import now_shanghai
        with ids["factory"]() as db:
            db.get(Channel, channel_id).deleted_at = now_shanghai()
            db.commit()
    else:
        address = address.rsplit("/", 1)[0] + "/invalid"
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(address + "/ws"):
            pass
    assert closed.value.code == 1008


def test_rejects_wrong_identity_and_accepts_same_device_reconnect(bridge_context):
    client, ids, _ = bridge_context
    channel_id, address = _start(bridge_context)
    for identity, expected in [(IDENTITY, True), ({**IDENTITY, "account_id": "wxid_other"}, False), ({**IDENTITY, "session_id": "restarted-session"}, True)]:
        with client.websocket_connect(address + "/ws") as socket:
            socket.send_json({**identity, "type": "hello"})
            if expected:
                assert socket.receive_json()["type"] == "hello_ack"
            else:
                with pytest.raises(WebSocketDisconnect):
                    socket.receive_json()


def test_events_before_ack_and_reconnect_deduplicate(bridge_context, monkeypatch):
    client, ids, _ = bridge_context
    channel_id, address = _start(bridge_context)
    ingested = []
    original_ingest = ws.ingest_inbound_event

    def ingest(*args, **kwargs):
        result = original_ingest(*args, **kwargs)
        ingested.append(result)
        return result

    monkeypatch.setattr(ws, "ingest_inbound_event", ingest)
    with client.websocket_connect(address + "/ws") as socket:
        socket.send_json({**IDENTITY, "type": "hello"})
        assert socket.receive_json()["type"] == "hello_ack"
        assert _confirm(bridge_context, channel_id).status_code == 303
    frame = {**IDENTITY, "type": "event", "event_id": "opaque-event:ws:001", "event_name": "message", "captured_at_ms": 1775000000100, "enqueued_at_ms": 1775000000120, "body": {"message_id": "ws-test-001", "sender_id": "wxid_test", "chat_id": "wxid_test", "content": "测试接收", "msg_type": "text", "is_group": False}}
    for iteration in range(2):
        with client.websocket_connect(address + "/ws") as socket:
            socket.send_json(frame)
            socket.send_json({**IDENTITY, "type": "hello"})
            assert socket.receive_json()["type"] == "hello_ack"
            deadline = time.monotonic() + 5
            while True:
                if len(ingested) == iteration + 1:
                    break
                assert time.monotonic() < deadline, "event not persisted"
                time.sleep(0.02)
    with ids["factory"]() as db:
        events = db.scalars(select(RawWebhookEvent).where(RawWebhookEvent.channel_id == channel_id)).all()
        assert len(events) == 1
        assert ingested[1]["deduplicated"] is True


def test_hello_timeout_closes_without_invented_error_frame(bridge_context, monkeypatch):
    client, _, _ = bridge_context
    _, address = _start(bridge_context)
    monkeypatch.setattr(ws, "HELLO_TIMEOUT_SECONDS", 0.03)
    with client.websocket_connect(address + "/ws") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1008


def test_disconnect_does_not_requeue_claimed_commands(bridge_context):
    client, ids, _ = bridge_context
    channel_id, address = _start(bridge_context)
    with client.websocket_connect(address + "/ws") as socket:
        socket.send_json({**IDENTITY, "type": "hello"})
        assert socket.receive_json()["type"] == "hello_ack"
        command = _command(socket, "account_status")
    with ids["factory"]() as db:
        jobs = db.scalars(select(TaskJob).where(TaskJob.status == "running")).all()
        claimed = next(job for job in jobs if command["request_id"] in job.payload_json)
        assert claimed.attempts == 1


def test_oversize_command_fails_locally_and_connection_continues(bridge_context, monkeypatch):
    client, ids, _ = bridge_context
    channel_id, address = _start(bridge_context)
    monkeypatch.setattr(ws, "MAX_FRAME_BYTES", 1)
    with client.websocket_connect(address + "/ws") as socket:
        socket.send_json({**IDENTITY, "type": "hello"})
        assert socket.receive_json()["type"] == "hello_ack"
        deadline = time.monotonic() + 5
        while True:
            with ids["factory"]() as db:
                failed = db.scalars(select(TaskJob).where(TaskJob.status == "failed")).all()
                if failed:
                    assert failed[0].error_message == "webridge_ws_frame_exceeds_10_mib"
                    break
            assert time.monotonic() < deadline
            time.sleep(0.02)


def test_missing_application_pong_closes_connection(bridge_context, monkeypatch):
    client, _, _ = bridge_context
    _, address = _start(bridge_context)
    monkeypatch.setattr(ws, "PONG_TIMEOUT_SECONDS", 0.05)
    with client.websocket_connect(address + "/ws") as socket:
        socket.send_json({**IDENTITY, "type": "hello"})
        assert socket.receive_json()["type"] == "hello_ack"
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                socket.receive_json()
        assert closed.value.code == 1008
