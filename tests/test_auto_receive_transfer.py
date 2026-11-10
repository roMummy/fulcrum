"""自动收款:账号级开关、转账事件触发与过滤、命令入队与去重、结果记为完成(并区分未确认)、设置页。"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="auto-receive-logs-"))

import pytest
from sqlalchemy import select

from app.models.entities import Channel, TaskJob
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.wechat_webridge import auto_receive, service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "wxid_bot"}


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    auto_receive.set_enabled(channel, True)
    db.commit()
    return db, channel


def _event(*, transfer_id="tx-1", message_id="m-1", sender="wxid_alice", chat_type="direct", message_type="transfer", **payment) -> dict:
    return {
        "conversation": {"external_id": "wxid_alice" if chat_type == "direct" else "1@chatroom", "chat_type": chat_type},
        "message": {"content": "transfer_received", "sender_id": sender, "message_type": message_type},
        "details": {"payment": {"transfer_id": transfer_id, "message_id": message_id, "amount_text": "￥0.02", **payment}},
    }


def _jobs(db) -> list[TaskJob]:
    return list(db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all())


# ---------- 开关 ----------

def test_switch_defaults_off_and_keeps_other_adapter_settings() -> None:
    channel = Channel(id=1, tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge", adapter_config_json=json.dumps({"upload_url": "http://x/up"}))
    assert auto_receive.is_enabled(channel) is False
    auto_receive.set_enabled(channel, True)
    assert auto_receive.is_enabled(channel) is True and json.loads(channel.adapter_config_json) == {"upload_url": "http://x/up", "auto_receive_transfer": True}
    auto_receive.set_enabled(channel, False)
    assert auto_receive.is_enabled(channel) is False
    # 配置损坏时按关闭处理,不抛异常
    channel.adapter_config_json = "{坏的"
    assert auto_receive.is_enabled(channel) is False


# ---------- 触发 ----------

def test_transfer_to_the_account_queues_one_high_priority_receive_command(env) -> None:
    db, channel = env
    with patch.object(wakeup, "notify") as notify:
        assert auto_receive.handle_event(db, channel, _event(), conversation_id=3) is True
    job = _jobs(db)[0]
    command = json.loads(job.payload_json)["command"]
    # 参数按文档:transfer_id 与 message_id 都传
    assert command["type"] == "receive_transfer" and command["params"] == {"transfer_id": "tx-1", "message_id": "m-1"}
    assert job.priority == auto_receive.PRIORITY and job.status == "pending"
    notify.assert_called_once_with(channel.id)  # 提交之后立刻唤醒手机轮询


def test_works_for_group_transfers_too_and_one_id_is_enough(env) -> None:
    db, channel = env
    with patch.object(wakeup, "notify"):
        assert auto_receive.handle_event(db, channel, _event(chat_type="group", transfer_id="tx-g", message_id=""), conversation_id=4) is True
    assert json.loads(_jobs(db)[0].payload_json)["command"]["params"] == {"transfer_id": "tx-g"}


def test_a_transfer_is_received_only_once(env) -> None:
    db, channel = env
    with patch.object(wakeup, "notify") as notify:
        assert auto_receive.handle_event(db, channel, _event()) is True
        assert auto_receive.handle_event(db, channel, _event()) is False  # 事件重复投递
    assert len(_jobs(db)) == 1 and notify.call_count == 1


def test_nothing_happens_when_off_or_not_a_transfer_or_own_transfer(env) -> None:
    db, channel = env
    with patch.object(wakeup, "notify") as notify:
        assert auto_receive.handle_event(db, channel, _event(message_type="text")) is False
        assert auto_receive.handle_event(db, channel, _event(transfer_id="", message_id="")) is False
        # 自己付出的转账(付款方或发送者是机器人账号)不收
        assert auto_receive.handle_event(db, channel, _event(sender="wxid_bot")) is False
        assert auto_receive.handle_event(db, channel, _event(transfer_id="tx-2", from_user="wxid_bot")) is False
        auto_receive.set_enabled(channel, False)
        assert auto_receive.handle_event(db, channel, _event(transfer_id="tx-3")) is False
    assert _jobs(db) == [] and not notify.called


def test_other_channel_types_are_never_touched(env) -> None:
    db, channel = env
    channel.channel_type = "wechat_ipad"
    assert auto_receive.handle_event(db, channel, _event()) is False and _jobs(db) == []


def test_adapter_passes_the_fields_needed_to_receive_and_to_tell_who_paid() -> None:
    adapter = WechatWeBridgeAdapter()
    normalized = adapter.normalize_inbound_payload(
        None,
        {"event_name": "transfer_received", "event_id": "transfer:tx-9", "body": {
            "chat_id": "wxid_alice", "transfer_id": "tx-9", "message_id": "m-9", "from_user": "wxid_alice", "to_user": "wxid_bot",
            "amount_fen": 2, "amount_text": "￥0.02",
        }},
    )
    payment = normalized["details"]["payment"]
    assert normalized["message"]["message_type"] == "transfer"
    assert (payment["transfer_id"], payment["message_id"], payment["from_user"], payment["to_user"], payment["amount_fen"]) == ("tx-9", "m-9", "wxid_alice", "wxid_bot", 2)


# ---------- 结果 ----------

@pytest.mark.parametrize(
    "reported, succeeded",
    [
        ({"ok": True, "result": {"ok": True, "status": "callback_ok", "accepted": True, "confirmed": True}}, True),
        # 文档:等待回调超时,命令发出了但没确认到账——ok/accepted 为真也不算收到
        ({"ok": True, "result": {"ok": True, "status": "confirm_invoked", "accepted": True, "confirmed": False}}, False),
        ({"ok": False, "result": {"ok": False, "status": "failed", "error_code": "NOT_RECEIVER"}}, False),
    ],
)
def test_results_are_business_outcomes_and_only_confirmed_counts_as_received(env, reported, succeeded) -> None:
    db, channel = env
    with patch.object(wakeup, "notify"):
        auto_receive.handle_event(db, channel, _event())
    job = _jobs(db)[0]
    job.status = "running"
    db.commit()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        answer = service.accept_command_result(db, channel, {**IDENTITY, "request_id": "transfer_tx-1", **reported})
    assert answer["status"] == "completed"
    db.refresh(job)
    assert job.status == "completed" and json.loads(job.result_json)["webridge"]["succeeded"] is succeeded


# ---------- 设置页 ----------

@pytest.fixture()
def webridge_channel(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    from app.models.entities import Channel as ChannelRow

    with ids["factory"]() as db:
        channel = ChannelRow(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.commit()
        channel_id = int(channel.id)
    return client, ids, channel_id


def _stored(ids, channel_id):
    with ids["factory"]() as db:
        return auto_receive.is_enabled(db.get(Channel, channel_id))


def test_admin_can_turn_the_switch_on_and_off_from_the_account_page(webridge_channel) -> None:
    client, ids, channel_id = webridge_channel
    csrf = _login(client, "admin")
    page = client.get(f"/channels/{channel_id}")
    assert page.status_code == 200 and "自动收款" in page.text and 'name="auto_receive_transfer"' in page.text and "checked" not in page.text.split('name="auto_receive_transfer"')[1][:80]

    on = client.post(f"/channels/{channel_id}/wechat-webridge/settings", data={"auto_receive_transfer": "on", "csrf_token": csrf})
    assert on.status_code == 303 and _stored(ids, channel_id) is True
    assert " checked" in client.get(f"/channels/{channel_id}").text.split('name="auto_receive_transfer"')[1][:80]

    # 勾选框没勾选时浏览器不提交该字段
    off = client.post(f"/channels/{channel_id}/wechat-webridge/settings", data={"csrf_token": csrf})
    assert off.status_code == 303 and _stored(ids, channel_id) is False


def test_only_tenant_admins_with_a_valid_csrf_token_can_change_it(webridge_channel) -> None:
    client, ids, channel_id = webridge_channel
    csrf = _login(client, "admin")
    assert client.post(f"/channels/{channel_id}/wechat-webridge/settings", data={"auto_receive_transfer": "on"}).status_code == 403  # 没有 CSRF
    assert _stored(ids, channel_id) is False
    csrf = _login(client, "operator")
    assert client.post(f"/channels/{channel_id}/wechat-webridge/settings", data={"auto_receive_transfer": "on", "csrf_token": csrf}).status_code == 403
    assert _stored(ids, channel_id) is False


# ---------- 接入后台调度 ----------

def test_the_dispatch_job_runs_auto_receive_before_plugins_and_survives_its_failure(env) -> None:
    """后台调度任务一收到转账事件就入队收款命令;自动收款自己出错不能拖垮后面的插件调度。"""

    from app.models.entities import Conversation
    from app.services import async_workers

    db, channel = env
    db.add(Conversation(id=3, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="wxid_alice", chat_type="direct"))
    db.commit()
    payload = {"channel_id": channel.id, "conversation_id": 3, "raw_event_id": None, "message_id": None, "normalized_event": _event()}
    with patch.object(wakeup, "notify") as notify:
        async_workers._process_plugin_dispatch_job(db, payload)
    assert [json.loads(job.payload_json)["command"]["type"] for job in _jobs(db)] == ["receive_transfer"] and notify.call_count == 1

    # 自动收款抛异常:调度照常完成(只记日志)
    with patch.object(auto_receive, "handle_event", side_effect=RuntimeError("boom")):
        result = async_workers._process_plugin_dispatch_job(db, {**payload, "normalized_event": _event(transfer_id="tx-2")})
    assert result is not None
