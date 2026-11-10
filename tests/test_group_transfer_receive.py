"""自动收款(群功能):触发与过滤、命令入队与去重、结果记为完成(并区分未确认)、接入后台调度、群页面里的卡片。"""

from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="transfer-receive-logs-"))

import pytest
from sqlalchemy import select

from app.models.entities import Chatroom, Conversation, TaskJob
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.group_features import transfer_receive
from app.services.group_features.dispatch import dispatch_group_features
from app.services.group_features.registry import get_feature
from app.services.group_features.store import save_state
from app.services.wechat_webridge import service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "wxid_bot"}


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="1@chatroom", name="养生群", is_active=True)
    db.add_all([
        room,
        Conversation(id=3, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="group", title="养生群"),
    ])
    db.commit()
    save_state(room, "transfer_receive", enabled=True)
    db.commit()
    return db, channel, room


def _event(*, transfer_id="tx-1", message_id="m-1", sender="wxid_alice", message_type="transfer", **payment) -> dict:
    return {
        "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
        "message": {"content": "transfer_received", "sender_id": sender, "sender_name": "爱丽丝", "message_type": message_type},
        "details": {"payment": {"transfer_id": transfer_id, "message_id": message_id, "amount_text": "￥0.02", **payment}},
    }


def _jobs(db) -> list[TaskJob]:
    return list(db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all())


def _dispatch(db, channel, event) -> list[str]:
    return dispatch_group_features(db, channel, event, conversation_id=3)


# ---------- 功能声明 ----------

def test_feature_is_a_switch_only_group_feature_for_webridge() -> None:
    feature = get_feature("transfer_receive")
    assert feature.name == "自动收款" and feature.channel_types == ("wechat_webridge",) and feature.event_types == ("transfer",)
    assert feature.fields == ()  # 只有开关:点一下就保存,没有需要"保存"的输入项


# ---------- 触发 ----------

def test_transfer_in_the_group_queues_one_high_priority_receive_command(env) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify") as notify:
        assert _dispatch(db, channel, _event()) == ["transfer_receive"]
    job = _jobs(db)[0]
    command = json.loads(job.payload_json)["command"]
    # 参数按文档:transfer_id 与 message_id 都传
    assert command["type"] == "receive_transfer" and command["params"] == {"transfer_id": "tx-1", "message_id": "m-1"}
    assert job.priority == transfer_receive.PRIORITY and job.status == "pending"
    notify.assert_called_once_with(channel.id)  # 提交之后立刻唤醒手机轮询


def test_one_id_is_enough(env) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify"):
        _dispatch(db, channel, _event(transfer_id="tx-g", message_id=""))
    assert json.loads(_jobs(db)[0].payload_json)["command"]["params"] == {"transfer_id": "tx-g"}


def test_a_transfer_is_received_only_once(env) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify") as notify:
        _dispatch(db, channel, _event())
        _dispatch(db, channel, _event())  # 事件重复投递
    assert len(_jobs(db)) == 1 and notify.call_count == 1


def test_nothing_happens_when_off_or_group_not_enabled_or_not_a_transfer_or_own_transfer(env) -> None:
    db, channel, room = env
    with patch.object(wakeup, "notify") as notify:
        assert _dispatch(db, channel, _event(message_type="text")) == []
        assert _dispatch(db, channel, _event(transfer_id="", message_id="")) == []
        # 自己付出的转账(付款方或发送者是机器人账号)不收
        _dispatch(db, channel, _event(sender="wxid_bot"))
        _dispatch(db, channel, _event(transfer_id="tx-2", from_user="wxid_bot"))
        save_state(room, "transfer_receive", enabled=False)
        assert _dispatch(db, channel, _event(transfer_id="tx-3")) == []
        save_state(room, "transfer_receive", enabled=True)
        room.is_active = False  # 群"启用"总开关关着:群功能一律不执行
        assert _dispatch(db, channel, _event(transfer_id="tx-4")) == []
    assert _jobs(db) == [] and not notify.called


def test_other_channel_types_are_never_touched(env) -> None:
    db, channel, _room = env
    channel.channel_type = "wechat_ipad"
    assert _dispatch(db, channel, _event()) == [] and _jobs(db) == []


def test_adapter_passes_the_fields_needed_to_receive_and_to_tell_who_paid() -> None:
    normalized = WechatWeBridgeAdapter().normalize_inbound_payload(
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
    db, channel, _room = env
    with patch.object(wakeup, "notify"):
        _dispatch(db, channel, _event())
    job = _jobs(db)[0]
    job.status = "running"
    db.commit()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        answer = service.accept_command_result(db, channel, {**IDENTITY, "request_id": "transfer_tx-1", **reported})
    assert answer["status"] == "completed"
    db.refresh(job)
    assert job.status == "completed" and json.loads(job.result_json)["webridge"]["succeeded"] is succeeded


# ---------- 接入后台调度 ----------

def test_the_dispatch_job_handles_the_transfer_and_survives_a_failing_feature(env) -> None:
    """后台调度任务经群功能框架处理转账;某个群功能抛异常不能拖垮后面的插件调度。"""

    from app.services import async_workers

    db, channel, _room = env
    payload = {"channel_id": channel.id, "conversation_id": 3, "raw_event_id": None, "message_id": None, "normalized_event": _event()}
    with patch.object(wakeup, "notify") as notify:
        async_workers._process_plugin_dispatch_job(db, payload)
    assert [json.loads(job.payload_json)["command"]["type"] for job in _jobs(db)] == ["receive_transfer"] and notify.call_count == 1

    # 处理函数抛异常:框架只记日志,调度照常完成
    from app.services.group_features import dispatch as dispatch_module

    original = dispatch_module._HANDLERS["transfer_receive"]
    dispatch_module._HANDLERS["transfer_receive"] = lambda ctx, args: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        result = async_workers._process_plugin_dispatch_job(db, {**payload, "normalized_event": _event(transfer_id="tx-2")})
    finally:
        dispatch_module._HANDLERS["transfer_receive"] = original
    assert result is not None


# ---------- 页面 ----------

@pytest.fixture()
def webridge_group(provider_page_context):  # noqa: F811
    from app.models.entities import Channel

    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.flush()
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, external_id="1@chatroom", name="养生群", is_active=True)
        db.add(room)
        db.commit()
        return client, ids, int(channel.id), int(room.id)


def test_group_page_has_the_feature_card_and_the_account_page_no_longer_has_a_switch(webridge_group) -> None:
    client, _ids, channel_id, room_id = webridge_group
    _login(client, "admin")
    group_html = client.get(f"/channels/{channel_id}/chatrooms/{room_id}").text
    assert "<h3>自动收款</h3>" in group_html and 'data-gf-key="transfer_receive"' in group_html
    account_html = client.get(f"/channels/{channel_id}").text
    # 转账收款只在群功能里设置,账号详情页不再有账号级开关
    assert "自动收款" not in account_html and 'name="auto_receive_transfer"' not in account_html
    assert client.post(f"/channels/{channel_id}/wechat-webridge/settings", data={"auto_receive_transfer": "on"}).status_code in {404, 405}
