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
from app.models.entities import BotPlugin, Conversation, Message, Plugin, TaskJob
from app.services import outbound_files, plugin_directory
from app.services.plugin_runtime.replies import PluginReplyError, music_reply, video_reply
from app.services.safe_fetch import UnsafeUrlError
from app.services.wechat_webridge import service
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed

IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "a1"}


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    parser = Plugin(code="douyin_parser", display_name="抖音解析")
    db.add(parser)
    db.flush()
    db.add(BotPlugin(tenant_id=1, bot_id=channel.bot_id, plugin_id=parser.id, status="enabled"))
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


def _video_context(plugin_code, channel, conversation):
    return SimpleNamespace(plugin_code=plugin_code, channel_type=channel.channel_type, normalized_event={},
                           binding_config={}, conversation_id=conversation.id, message_type="text", sender_id="demo",
                           conversation_external_id=conversation.external_id, chat_type=conversation.chat_type)


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


@pytest.mark.parametrize("plugin_code", ["clips", "douyin_parser"])
def test_plugin_remote_video_reaches_webridge_poll_without_file(env, tmp_path, monkeypatch, plugin_code):
    db, channel, conversation = env
    url = "https://media.example.org/play/?id=12&sig=a%2Fb%3D"
    ctx = _video_context(plugin_code, channel, conversation)
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", tmp_path)
    # 只模拟 DNS 和无关的设备维护；任务入库、领取与参数映射走真实代码。
    with patch("app.services.safe_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
        result, = plugin_directory._dispatch_auto_outbound_messages(
            db, SimpleNamespace(channel_id=channel.id), ctx, {"replies": [video_reply(video_url=url)]})
    assert result["queued"]
    message = db.get(Message, result["message_id"])
    assert (message.message_type, message.external_status) == ("video", "pending")
    with patch.object(service, "_poll_maintenance"):
        command, = service.claim_commands(db, channel, IDENTITY)
    assert command["command_name"] == "send_video_from_url"
    expected = {"max_size_mb": 100} if plugin_code == "douyin_parser" else {}
    assert command["args"] == {"receiver": conversation.external_id, "source": url, **expected}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("plugin_code", "error", "expect_card"), [
    ("douyin_parser", "video exceeds max_size_bytes limit (104857601 > 104857600)", True),
    ("douyin_parser", "video exceeds max_size_bytes limit (104857600 > 104857600)", False),
    ("douyin_parser", "video exceeds max_size_bytes limit (11546502 > 10485760)", False),
    ("douyin_parser", "download timed out", False),
    ("clips", "video exceeds max_size_bytes limit (104857601 > 104857600)", False),
])
def test_remote_video_size_receipt(env, plugin_code, error, expect_card):
    db, channel, conversation = env
    conversation.chat_type = "group"
    url = "https://media.example.org/play/?sig=a%2Fb"
    ctx = _video_context(plugin_code, channel, conversation)
    with patch("app.services.safe_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         patch.object(service, "_poll_maintenance"):
        result, = plugin_directory._dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=channel.id), ctx, {
            "title": "测试作品", "author": "作者", "replies": [video_reply(video_url=url)]})
        first, = service.claim_commands(db, channel, IDENTITY)
        receipt = {**IDENTITY, "request_id": first["request_id"], "ok": False, "result": {"error": error}}
        assert service.accept_command_result(db, channel, receipt)["status"] == "failed"
        assert service.accept_command_result(db, channel, receipt)["deduplicated"]
        commands = service.claim_commands(db, channel, IDENTITY)
        message = db.get(Message, result["message_id"])
        if expect_card:
            card, = commands
            assert card["command_name"] == "send_link_card" and card["request_id"] != first["request_id"]
            assert card["args"] == {"receiver": conversation.external_id, "url": url,
                                    "title": "作者", "description": "测试作品", "thumb_url": ""}
            assert (message.message_type, message.external_status) == ("link", "pending")
            service.accept_command_result(db, channel, {**IDENTITY, "request_id": card["request_id"], "ok": True,
                                                        "result": {"message_id": "native-card"}})
            assert (message.external_status, message.external_message_id) == ("sent", "native-card")
            assert not service.claim_commands(db, channel, IDENTITY)
        else:
            assert not commands
            assert (message.message_type, message.external_status) == ("video", "failed")


def test_pending_size_card_obeys_group_plugin_disable(env):
    db, channel, conversation = env
    conversation.chat_type = "group"
    ctx = _video_context("douyin_parser", channel, conversation)
    with patch("app.services.safe_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         patch.object(service, "_poll_maintenance"):
        result, = plugin_directory._dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=channel.id), ctx, {
            "replies": [video_reply(video_url="https://media.example.org/demo")]})
        cmd, = service.claim_commands(db, channel, IDENTITY)
        service.accept_command_result(db, channel, {**IDENTITY, "request_id": cmd["request_id"], "ok": False,
            "error": "video exceeds max_size_bytes limit (104857601 > 104857600)"})
        parser = db.scalar(select(Plugin).where(Plugin.code == "douyin_parser"))
        parser.platform_status = "disabled"
        db.commit()
        assert not service.claim_commands(db, channel, IDENTITY)
        message = db.get(Message, result["message_id"])
        assert (message.message_type, message.external_status) == ("link", "failed")


def test_plain_outbound_json_cannot_enable_douyin_video_policy(env):
    db, channel, conversation = env
    url = "https://media.example.org/video"
    job = service.enqueue_message_command(db, channel, conversation, {"message_type": "video", "video_url": url,
                                                                      "oversize_card": {"title": "foreign"}})
    payload = json.loads(job.payload_json)
    assert payload["command"]["params"] == {"source": url, "receiver": conversation.external_id}
    assert "oversize_card" not in payload and "plugin_code" not in payload


def test_saved_plugin_limit_is_snapshotted_per_command(env):
    from app.services.plugin_schemas import build_plugin_config_view, merge_schema_config

    db, channel, conversation = env
    ctx = _video_context("douyin_parser", channel, conversation)
    fields = {f["name"]: f for f in build_plugin_config_view("douyin_parser", None)["fields"]}
    key = "webridge_video_limit_mib"
    assert fields[key]["default"] == 100 and fields[key]["minimum"] == 1
    assert fields["dtk_api_key"]["sensitive"] and {"download_mode", "keywords", "require_mention"} <= fields.keys()
    with patch("app.services.safe_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
         patch.object(service, "_poll_maintenance"):
        commands = []
        for value in ("12.5", "200"):
            saved = merge_schema_config("douyin_parser", {f"schema__{key}": value})
            ctx.binding_config = json.loads(saved.config_json)
            plugin_directory._dispatch_auto_outbound_messages(db, SimpleNamespace(channel_id=channel.id), ctx, {
                "author": "抖音作者",
                "title": "抖音文案",
                "cover": "https://img.example.org/cover.jpg",
                "replies": [video_reply(video_url="https://media.example.org/video")]})
            command, = service.claim_commands(db, channel, IDENTITY)
            assert command["args"]["max_size_mb"] == float(value)
            commands.append(command)
        # 当前配置已改成 200，第一条仍按其 12.5 MiB 回报转卡片，第二条不受影响。
        for command in commands:
            service.accept_command_result(db, channel, {**IDENTITY, "request_id": command["request_id"], "ok": False,
                "error": "video exceeds max_size_bytes limit (13107201 > 13107200)"})
        card, = service.claim_commands(db, channel, IDENTITY)
        assert card["command_name"] == "send_link_card"
        assert card["args"]["thumb_url"] == "https://img.example.org/cover.jpg"


def test_remote_video_rejects_invalid_sources_and_unsupported_channel():
    url = "https://media.example.org/play/"
    for invalid in ({"video_url": "file:///clip.mp4"}, {"video_url": "https://user:pass@media.example.org/"},
                    {"video_url": url, "base64": "QQ=="}):
        with pytest.raises(PluginReplyError):
            plugin_directory._normalize_reply_item({"type": "video", **invalid}, channel_type="wechat_webridge")
    with pytest.raises(PluginReplyError):
        plugin_directory._normalize_reply_item(video_reply(video_url=url), channel_type="wechat_ipad")
    with patch("app.services.safe_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
        with pytest.raises(UnsafeUrlError):
            plugin_directory._normalize_reply_item(video_reply(video_url=url), channel_type="wechat_webridge")


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


@pytest.mark.parametrize("sent", [True, False])
def test_music_card_is_queued_once_and_settled_by_phone_result(env, sent) -> None:
    db, channel, conversation = env
    context = SimpleNamespace(
        plugin_code="music", conversation_id=conversation.id, conversation_external_id=conversation.external_id,
        chat_type="direct", normalized_event={}, channel_type="wechat_webridge", message_type="text",
        binding_config={}, sender_id="wxid_a",
    )
    card = music_reply(
        title="晴天", desc="歌手", url="https://y.qq.com/n/ryqq/songDetail/song001",
        data_url="https://media.example.test/audio.mp3?vkey=temporary", song_lyric="[00:01.00]歌词",
        song_album_url="https://media.example.test/cover.jpg", app_id="test-app", app_name="音乐来源",
    )
    results = plugin_directory._dispatch_auto_outbound_messages(
        db, SimpleNamespace(channel_id=channel.id), context, {"replies": [card]},
    )
    assert len(results) == 1 and results[0]["queued"] is True
    job = _job_of(db, results[0])
    command = json.loads(job.payload_json)["command"]
    assert command["type"] == "send_music_card"
    assert command["params"] == {"receiver": conversation.external_id, **{key: value for key, value in card.items() if key != "type"}}
    message = db.get(Message, results[0]["message_id"])
    assert (message.message_type, message.external_status) == ("music", "pending")
    job.status = "running"
    db.commit()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service.accept_command_result(db, channel, {
            **IDENTITY, "request_id": command["request_id"], "ok": sent,
            "result": {"ok": sent, **({} if sent else {"error": "phone_send_failed"})},
        })
    assert (message.status, message.external_status) == (("sent", "sent") if sent else ("failed", "failed"))
    assert len(db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all()) == 1
