"""自动抢红包:触发与过滤、按延迟安排领取、去重、结果记为完成(不产生失败任务),以及长轮询唤醒。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="red-packet-logs-"))

import pytest
from sqlalchemy import select

from app.models.entities import Chatroom, Conversation, TaskJob
from app.services.group_features import red_packet
from app.services.group_features.dispatch import dispatch_group_features
from app.services.group_features.registry import get_feature
from app.services.group_features.store import get_state, save_state
from app.services.wechat_webridge import service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed

IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "wxid_bot"}
PACKET = "rp_msg_7307587870543245070"


@pytest.fixture()
def env(monkeypatch):
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="1@chatroom", name="群", is_active=True)
    conversation = Conversation(id=3, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="group")
    db.add_all([room, conversation])
    db.commit()
    save_state(room, "red_packet", enabled=True, config={"delay_ms": 800})
    db.commit()

    scheduled: list[tuple[float, tuple]] = []
    monkeypatch.setattr(red_packet, "_schedule", lambda delay, func, *args: scheduled.append((delay, args)))

    @contextmanager
    def fake_session(_factory):
        yield db

    monkeypatch.setattr(red_packet, "tenant_session", fake_session)
    return db, channel, room, scheduled


def _event(packet: str = PACKET, sender: str = "wxid_other", message_type: str = "red_packet") -> dict:
    return {
        "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
        "message": {"content": "red_packet_received", "sender_id": sender, "message_type": message_type},
        "details": {"payment": {"red_packet_id": packet, "send_id": "1000039501", "channel_id": "1"} if packet else {}},
    }


# ---------- 功能声明 ----------

def test_feature_is_webridge_only_with_a_bounded_delay_field() -> None:
    feature = get_feature("red_packet")
    assert feature.channel_types == ("wechat_webridge",) and feature.event_types == ("red_packet",)
    delay = feature.fields[0]
    assert (delay.key, delay.kind, delay.minimum, delay.maximum) == ("delay_ms", "number", 0, 60_000)


def test_default_is_off_and_delay_is_clamped(env) -> None:
    db, _channel, room, _scheduled = env
    save_state(room, "red_packet", enabled=True, config={"delay_ms": 999_999})
    assert get_state(room, get_feature("red_packet"))[1]["delay_ms"] == 60_000
    save_state(room, "red_packet", enabled=True, config={"delay_ms": -5})
    assert get_state(room, get_feature("red_packet"))[1]["delay_ms"] == 0


# ---------- 触发与过滤 ----------

def test_red_packet_event_schedules_a_claim_after_the_configured_delay(env) -> None:
    db, channel, _room, scheduled = env
    executed = dispatch_group_features(db, channel, _event(), conversation_id=3)
    assert executed == ["red_packet"]
    delay, args = scheduled[0]
    # 延迟按毫秒配置、按秒交给定时器;领取参数带上租户/渠道/会话和红包 ID
    assert delay == pytest.approx(0.8) and args == (1, channel.id, 3, PACKET)


def test_nothing_happens_when_the_switch_is_off_or_the_group_is_not_enabled(env) -> None:
    db, channel, room, scheduled = env
    save_state(room, "red_packet", enabled=False)
    assert dispatch_group_features(db, channel, _event(), conversation_id=3) == []
    save_state(room, "red_packet", enabled=True)
    room.is_active = False  # 群"启用"总开关关着:群功能一律不执行
    assert dispatch_group_features(db, channel, _event(), conversation_id=3) == []
    assert scheduled == []


def test_ordinary_messages_events_without_an_id_and_own_packets_are_ignored(env) -> None:
    db, channel, _room, scheduled = env
    assert dispatch_group_features(db, channel, _event(message_type="text"), conversation_id=3) == []
    assert dispatch_group_features(db, channel, _event(packet=""), conversation_id=3) == []
    # 机器人账号自己发的红包不领(领自己的只会得到一次必然失败的命令)
    assert dispatch_group_features(db, channel, _event(sender="wxid_bot"), conversation_id=3) == ["red_packet"]
    assert scheduled == []


# ---------- 到点入队 ----------

def test_claim_enqueues_one_high_priority_command_and_wakes_the_poll(env) -> None:
    db, channel, _room, _scheduled = env
    with patch.object(wakeup, "notify") as notify:
        red_packet.claim(1, channel.id, 3, PACKET)
        red_packet.claim(1, channel.id, 3, PACKET)  # 重复事件:同一个红包只领一次
    jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all()
    assert len(jobs) == 1
    command = json.loads(jobs[0].payload_json)["command"]
    assert command["type"] == "receive_red_packet" and command["params"] == {"red_packet_id": PACKET}
    assert jobs[0].priority == red_packet.CLAIM_PRIORITY and jobs[0].status == "pending"
    notify.assert_called_once_with(channel.id)


def test_claim_for_a_deleted_channel_does_nothing(env) -> None:
    from app.core.time import now_shanghai

    db, channel, _room, _scheduled = env
    channel.deleted_at = now_shanghai()
    db.commit()
    with patch.object(wakeup, "notify") as notify:
        red_packet.claim(1, channel.id, 3, PACKET)
    assert db.scalars(select(TaskJob)).all() == [] and not notify.called


# ---------- 领取结果 ----------

def _claim_job(db, channel) -> TaskJob:
    with patch.object(wakeup, "notify"):
        red_packet.claim(1, channel.id, 3, PACKET)
    job = db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).one()
    job.status = "running"
    db.commit()
    return job


@pytest.mark.parametrize(
    "reported, claimed",
    [
        ({"ok": True, "result": {"ok": True, "status": "received", "amount_text": "0.88"}}, True),
        ({"ok": False, "result": {"ok": False, "status": "fully_claimed"}}, False),  # 来晚了:被领完了
    ],
)
def test_claim_results_are_business_outcomes_and_never_failed_jobs(env, reported, claimed) -> None:
    db, channel, _room, _scheduled = env
    job = _claim_job(db, channel)
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        answer = service.accept_command_result(db, channel, {**IDENTITY, "request_id": f"redpacket_{PACKET}", **reported})
    assert answer["status"] == "completed"
    db.refresh(job)
    # 不记成失败任务,否则每次"来晚了"都会触发管理后台的失败任务告警
    assert job.status == "completed" and json.loads(job.result_json)["webridge"]["succeeded"] is claimed


# ---------- 长轮询唤醒 ----------

def test_notify_wakes_a_waiting_poll_from_another_thread_and_delayed_notify_waits() -> None:
    async def scenario() -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        wakeup.register(7, loop, event)
        try:
            started = time.monotonic()
            threading.Thread(target=wakeup.notify, args=(7,)).start()
            await asyncio.wait_for(event.wait(), timeout=2)
            immediate = time.monotonic() - started
            event.clear()
            started = time.monotonic()
            wakeup.notify(7, 0.15)  # 延迟通知:到点才叫醒
            await asyncio.wait_for(event.wait(), timeout=2)
            return immediate, time.monotonic() - started
        finally:
            wakeup.unregister(7, loop, event)

    immediate, delayed = asyncio.run(scenario())
    assert immediate < 0.1 and 0.12 <= delayed < 0.5
    assert wakeup._waiters == {}  # 登记已清理,不会泄漏


def test_notify_only_wakes_the_matching_channel() -> None:
    async def scenario() -> bool:
        loop = asyncio.get_running_loop()
        other = asyncio.Event()
        wakeup.register(8, loop, other)
        try:
            wakeup.notify(9)
            await asyncio.sleep(0.05)
            return other.is_set()
        finally:
            wakeup.unregister(8, loop, other)

    assert asyncio.run(scenario()) is False


# ---------- 轮询接口本身(所有手机都走这条路径,改动必须有直接覆盖) ----------

class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture()
def poll_env(monkeypatch):
    from app.api import wechat_webridge as api

    async def fake_json(_request):
        return {}

    monkeypatch.setattr(api, "_json", fake_json)
    monkeypatch.setattr(api, "_channel_tenant", lambda channel_id: (1, 1))
    return api


def test_poll_returns_as_soon_as_it_is_woken_instead_of_waiting_for_the_next_check(poll_env, monkeypatch) -> None:
    api = poll_env
    queue: list[dict] = []
    monkeypatch.setattr(api, "_claim_once", lambda channel_id, tenant_id, body: [queue.pop()] if queue else [])
    # 检查间隔调得很长:如果不是被唤醒而是靠定时检查,这个测试会超时
    monkeypatch.setattr(api, "POLL_CHECK_INTERVAL_SECONDS", 5)

    async def scenario() -> tuple[dict, float]:
        task = asyncio.create_task(api.poll(11, _FakeRequest()))
        await asyncio.sleep(0.1)  # poll 已经挂起
        queue.append({"command_name": "receive_red_packet"})
        started = time.monotonic()
        wakeup.notify(11)
        result = await asyncio.wait_for(task, timeout=2)
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(scenario())
    assert result == {"commands": [{"command_name": "receive_red_packet"}]} and elapsed < 0.5
    assert wakeup._waiters == {}


def test_poll_still_returns_empty_at_the_deadline_and_cleans_up(poll_env, monkeypatch) -> None:
    api = poll_env
    monkeypatch.setattr(api, "_claim_once", lambda channel_id, tenant_id, body: [])
    monkeypatch.setattr(api, "POLL_HOLD_SECONDS", 0.3)
    monkeypatch.setattr(api, "POLL_CHECK_INTERVAL_SECONDS", 0.1)
    started = time.monotonic()
    assert asyncio.run(api.poll(12, _FakeRequest())) == {"commands": []}
    assert 0.3 <= time.monotonic() - started < 1.5 and wakeup._waiters == {}


def test_a_notification_arriving_between_the_check_and_the_wait_is_not_lost(poll_env, monkeypatch) -> None:
    """查询返回空之后、开始等待之前来的通知不能丢(否则命令要多等一整个检查间隔)。"""

    api = poll_env
    calls = {"n": 0}

    def claim(channel_id, tenant_id, body):
        calls["n"] += 1
        if calls["n"] == 1:
            wakeup.notify(13)  # 模拟:第一次查询刚结束,入队方恰好此时通知
            return []
        return [{"command_name": "x"}]

    monkeypatch.setattr(api, "_claim_once", claim)
    monkeypatch.setattr(api, "POLL_CHECK_INTERVAL_SECONDS", 5)
    started = time.monotonic()
    assert asyncio.run(api.poll(13, _FakeRequest())) == {"commands": [{"command_name": "x"}]}
    assert time.monotonic() - started < 1


def test_feature_card_only_shows_in_webridge_groups_with_a_millisecond_field(env) -> None:
    from app.services.group_features.view import build_feature_cards

    _db, _channel, room, _scheduled = env
    keys = lambda channel_type: [card["key"] for card in build_feature_cards(room, channel_type)]
    assert "red_packet" in keys("wechat_webridge") and "red_packet" not in keys("wechat_ipad")
    card = next(item for item in build_feature_cards(room, "wechat_webridge") if item["key"] == "red_packet")
    assert card["enabled"] is True and card["name"] == "自动抢红包"
    field = card["sections"][0]["inputs"][0]
    assert (field["name"], field["kind"], field["value"], field["minimum"], field["maximum"]) == ("cfg_delay_ms", "number", 800, 0, 60_000)
