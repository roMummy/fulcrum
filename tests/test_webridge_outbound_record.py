"""微桥出站消息落库:插件/AI 的自动回复要留下消息记录,手机回报成功、失败或超时后更新状态。

之前微桥分支直接入队命令,没有任何消息记录(message_id 一直是 None),后台消息中心里完全看不到机器人发出的消息。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-outbound-logs-"))

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Conversation, Message, TaskJob
from app.services import plugin_directory
from app.services.wechat_webridge import service
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed

IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "a1"}


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    conversation = Conversation(id=3, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="direct")
    db.add(conversation)
    db.commit()
    return db, channel, conversation


def _auto_reply(db, channel, conversation, text: str) -> dict:
    """走插件/AI 自动回复的出站入口,返回它给出的结果项。"""

    context = SimpleNamespace(
        plugin_code="message_sign", conversation_id=conversation.id, conversation_external_id=conversation.external_id,
        chat_type="direct", normalized_event={}, channel_type="wechat_webridge", message_type="text",
        binding_config={}, sender_id="wxid_a",
    )
    results = plugin_directory._dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=channel.id), context, {"reply_text": text})
    assert len(results) == 1
    return results[0]


def _job_of(db, result: dict) -> TaskJob:
    return db.get(TaskJob, int(result["job_id"]))


def _request_id(job: TaskJob) -> str:
    return json.loads(job.payload_json)["command"]["request_id"]


def test_auto_reply_leaves_an_outbound_message_record_linked_to_the_command(env) -> None:
    db, channel, conversation = env
    result = _auto_reply(db, channel, conversation, "签到成功，积分 +5")
    assert result["ok"] is True and result["queued"] is True and result["message_id"]
    message = db.get(Message, result["message_id"])
    assert (message.direction, message.content, message.message_type, message.conversation_id) == ("out", "签到成功，积分 +5", "text", conversation.id)
    assert message.external_status == "pending"
    # 命令任务存在,并且消息里记着它的 request_id,手机回报时据此找回这条消息
    job = _job_of(db, result)
    assert job.job_type == service.JOB_TYPE and json.loads(job.payload_json)["command"]["type"] == "send_message"
    assert _request_id(job) in message.raw_request_json
    # 会话的最后消息时间随之更新(消息中心的会话列表按它排序)
    assert db.get(Conversation, conversation.id).last_message_at is not None


def test_unsupported_payload_still_leaves_a_failed_record_and_reports_the_error(env) -> None:
    db, channel, conversation = env
    context = SimpleNamespace(
        plugin_code="x", conversation_id=conversation.id, conversation_external_id=conversation.external_id, chat_type="direct",
        normalized_event={}, channel_type="wechat_webridge", message_type="text", binding_config={}, sender_id="wxid_a",
    )
    output = {"outbound_messages": [{"message_type": "hologram", "content": "?"}]}
    result = plugin_directory._dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=channel.id), context, output)[0]
    assert result["ok"] is False and "unsupported" in result["error"]
    assert db.get(Message, result["message_id"]).status == "failed"


def test_phone_result_settles_the_message_status(env) -> None:
    db, channel, conversation = env
    ok_result = _auto_reply(db, channel, conversation, "第一条")
    failed_result = _auto_reply(db, channel, conversation, "第二条")
    for result in (ok_result, failed_result):
        _job_of(db, result).status = "running"  # 手机已领取
    db.commit()

    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service.accept_command_result(db, channel, {**IDENTITY, "request_id": _request_id(_job_of(db, ok_result)), "ok": True, "result": {"ok": True}})
        service.accept_command_result(
            db, channel, {**IDENTITY, "request_id": _request_id(_job_of(db, failed_result)), "ok": False, "result": {"ok": False, "error": "not_logged_in"}}
        )
    sent, failed = db.get(Message, ok_result["message_id"]), db.get(Message, failed_result["message_id"])
    assert (sent.status, sent.external_status) == ("sent", "sent")
    assert (failed.status, failed.external_status) == ("failed", "failed") and "not_logged_in" in failed.raw_response_json


def test_command_timeout_marks_the_message_failed_and_other_messages_stay_untouched(env) -> None:
    db, channel, conversation = env
    stuck = _auto_reply(db, channel, conversation, "手机卡住了")
    waiting = _auto_reply(db, channel, conversation, "还在排队")
    job = _job_of(db, stuck)
    job.status = "running"
    job.locked_at = now_shanghai() - timedelta(seconds=service.COMMAND_RUNNING_TIMEOUT_SECONDS + 60)
    db.commit()
    assert service.reap_stale_commands(db, channel) == 1
    assert db.get(Message, stuck["message_id"]).status == "failed"
    assert db.get(Message, waiting["message_id"]).external_status == "pending"  # 没被领取的不受影响


def test_read_commands_without_a_message_do_not_touch_any_message(env) -> None:
    """读取类命令(目录、状态等)回报时,找不到对应消息就什么也不做。"""

    db, channel, conversation = env
    pending = _auto_reply(db, channel, conversation, "排队中的消息")
    job = service.enqueue_command(db, channel, command_name="room_members", args={})
    job.status = "running"
    db.commit()
    request_id = json.loads(job.payload_json)["command"]["request_id"]
    with patch.object(service, "persist_directory_result", return_value={}):
        service.accept_command_result(db, channel, {**IDENTITY, "request_id": request_id, "ok": True, "result": {"ok": True, "members": []}})
    assert db.get(Message, pending["message_id"]).external_status == "pending"
    assert db.scalars(select(Message).where(Message.direction == "out")).all().__len__() == 1
