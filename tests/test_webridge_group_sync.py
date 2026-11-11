"""微桥新群自动同步:bot 被拉进新群后,自动同步群资料和成员,不用手动点"同步群组"。

协议没有"本账号被拉进群"的专门事件,所以用三种触发(定期对账、未登记群的消息、入群事件/系统消息),
全部汇到去重 + 防抖的同一个入口;群列表结果回来后再为新群和还没有成员记录的群下发 room_members。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-group-sync-logs-"))

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Chatroom, ChatroomMember, TaskJob
from app.services.channel_events import ingest_inbound_event
from app.services.wechat_webridge import group_sync, service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed

IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "a1"}
NEW_ROOM = "20000000002@chatroom"
OLD_ROOM = "10000000001@chatroom"


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    db.commit()
    return db, channel


def _commands(db, name: str) -> list[tuple[TaskJob, dict]]:
    rows = []
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE).order_by(TaskJob.id)).all():
        command = json.loads(job.payload_json).get("command", {})
        if command.get("type") == name:
            rows.append((job, command))
    return rows


def _add_room(db, channel, external_id: str, *, members: int = 0, management_status: str = "enabled") -> Chatroom:
    room = Chatroom(tenant_id=1, bot_id=channel.bot_id, channel_id=channel.id, external_id=external_id, name="旧群", status="synced", management_status=management_status, is_active=True)
    db.add(room)
    db.flush()
    for number in range(members):
        db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=channel.bot_id, channel_id=channel.id, member_external_id=f"wxid_{number}", status="active"))
    db.commit()
    return room


def _event(*, event_type="message", message_type="text", content="hi"):
    return {"event_type": event_type, "message": {"message_type": message_type, "content": content}}


def _group(external_id: str, chat_type: str = "group"):
    return SimpleNamespace(chat_type=chat_type, external_id=external_id)


@pytest.fixture()
def woken():
    calls = []
    with patch.object(wakeup, "notify", lambda channel_id, delay=0.0: calls.append((channel_id, delay))):
        yield calls


# ---------- 入口:去重与防抖 ----------

def test_a_sync_request_is_deduplicated_while_one_is_pending_and_debounced_afterwards(env) -> None:
    db, channel = env
    first = group_sync.request_group_sync(db, channel, reason="unknown_group_message")
    db.commit()
    assert first is not None and len(_commands(db, "groups_all")) == 1
    assert group_sync.request_group_sync(db, channel, reason="member_join_event") is None  # 已有未完成的同步
    first.status = "completed"
    db.commit()
    assert group_sync.request_group_sync(db, channel, reason="member_join_event") is None  # 刚请求过,防抖
    state = json.loads(channel.runtime_info_json)["group_sync"]
    state["requested_at"] = (now_shanghai() - timedelta(seconds=group_sync.EVENT_MIN_GAP_SECONDS + 5)).isoformat()
    channel.runtime_info_json = json.dumps({**json.loads(channel.runtime_info_json), "group_sync": state})
    assert group_sync.request_group_sync(db, channel, reason="member_join_event") is not None
    assert len(_commands(db, "groups_all")) == 2


def test_periodic_reconcile_only_fires_when_the_interval_has_passed(env) -> None:
    db, channel = env
    group_sync.reconcile_if_due(db, channel)
    db.commit()
    assert len(_commands(db, "groups_all")) == 1
    job, _ = _commands(db, "groups_all")[0]
    job.status = "completed"
    db.commit()
    group_sync.reconcile_if_due(db, channel)  # 刚同步过:不到间隔
    assert len(_commands(db, "groups_all")) == 1
    runtime = json.loads(channel.runtime_info_json)
    runtime["group_sync"]["requested_at"] = (now_shanghai() - timedelta(seconds=group_sync.PERIODIC_SECONDS + 5)).isoformat()
    channel.runtime_info_json = json.dumps(runtime)
    group_sync.reconcile_if_due(db, channel)
    assert len(_commands(db, "groups_all")) == 2


# ---------- 入站事件触发 ----------

def test_a_message_from_an_unregistered_group_triggers_a_sync_and_wakes_the_phone(env, woken) -> None:
    db, channel = env
    group_sync.observe_inbound_event(db, channel, _event(), _group(NEW_ROOM))
    db.commit()
    assert len(_commands(db, "groups_all")) == 1
    assert woken == [(channel.id, 2.0)]  # 命令要等事务提交之后才查得到,所以延迟叫醒
    assert json.loads(channel.runtime_info_json)["group_sync"]["reason"] == "unknown_group_message"


def test_known_groups_and_private_chats_do_not_trigger_a_sync(env, woken) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM)
    group_sync.observe_inbound_event(db, channel, _event(), _group(OLD_ROOM))  # 已登记的群
    group_sync.observe_inbound_event(db, channel, _event(), _group("wxid_friend", chat_type="direct"))  # 私聊
    group_sync.observe_inbound_event(db, channel, _event(message_type="system", content="撤回了一条消息"), _group(OLD_ROOM))
    assert _commands(db, "groups_all") == [] and woken == []


@pytest.mark.parametrize(
    "event_type, message_type, content",
    [
        ("group_member_join", "group_member_join", "某人加入了群聊"),
        ("message", "system", '"小明"邀请你加入了群聊，群聊参与人还有：小红、小刚'),
        ("message", "system", '你被"小明"邀请加入群聊'),
        ("message", "system", "你已加入群聊"),
    ],
)
def test_join_events_and_invitation_system_messages_trigger_a_sync(env, woken, event_type, message_type, content) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM)  # 即使这个群已登记(比如被踢后又被拉回),入群信号也要同步一次
    group_sync.observe_inbound_event(db, channel, _event(event_type=event_type, message_type=message_type, content=content), _group(OLD_ROOM))
    db.commit()
    assert len(_commands(db, "groups_all")) == 1 and woken


# ---------- 群列表结果回来之后 ----------

def _finish_groups_all(db, channel, items):
    job, command = _commands(db, "groups_all")[-1]
    job.status = "running"
    db.commit()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service.accept_command_result(
            db, channel, {**IDENTITY, "request_id": command["request_id"], "ok": True, "result": {"ok": True, "total": len(items), "items": items}}
        )
    db.commit()


def test_a_new_group_is_created_and_its_members_are_requested(env) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM, members=3)  # 老群已有成员记录
    group_sync.request_group_sync(db, channel, reason="periodic", min_gap_seconds=0)
    db.commit()
    _finish_groups_all(db, channel, [
        {"account_id": OLD_ROOM, "nick_name": "老群", "member_count": 3},
        {"account_id": NEW_ROOM, "nick_name": "新拉进的群", "member_count": 12},
    ])
    room = db.scalar(select(Chatroom).where(Chatroom.external_id == NEW_ROOM))
    assert (room.name, room.member_count, room.management_status, room.is_active) == ("新拉进的群", 12, "enabled", True)  # 新群默认启用
    members = _commands(db, "room_members")
    assert [command["params"]["room_id"] for _, command in members] == [NEW_ROOM]  # 老群已有成员,不重复请求
    assert members[0][0].priority == group_sync.MEMBER_SYNC_PRIORITY
    state = json.loads(channel.runtime_info_json)["group_sync"]
    assert state["last_new_groups"] == 1 and NEW_ROOM in state["member_requested_at"]


def test_the_member_result_for_a_new_group_is_stored(env) -> None:
    db, channel = env
    group_sync.request_group_sync(db, channel, reason="periodic", min_gap_seconds=0)
    db.commit()
    _finish_groups_all(db, channel, [{"account_id": NEW_ROOM, "nick_name": "新拉进的群"}])
    job, command = _commands(db, "room_members")[0]
    job.status = "running"
    db.commit()
    members = [{"account_id": "wxid_a", "nick_name": "甲", "is_owner": True}, {"account_id": "wxid_b", "nick_name": "乙"}]
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service.accept_command_result(db, channel, {**IDENTITY, "request_id": command["request_id"], "ok": True, "result": {"ok": True, "room_id": NEW_ROOM, "members": members}})
    room = db.scalar(select(Chatroom).where(Chatroom.external_id == NEW_ROOM))
    rows = db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id)).all()
    assert sorted((m.member_external_id, m.role) for m in rows) == [("wxid_a", "owner"), ("wxid_b", "member")] and room.member_count == 2


def test_member_requests_are_capped_per_round_and_retried_only_after_the_backoff(env) -> None:
    db, channel = env
    for number in range(group_sync.MAX_MEMBER_SYNC_PER_ROUND + 4):
        _add_room(db, channel, f"{30000000000 + number}@chatroom")  # 都还没有成员记录
    group_sync.request_group_sync(db, channel, reason="periodic", min_gap_seconds=0)
    db.commit()
    _finish_groups_all(db, channel, [{"account_id": f"{30000000000 + n}@chatroom", "nick_name": "群"} for n in range(group_sync.MAX_MEMBER_SYNC_PER_ROUND + 4)])
    assert len(_commands(db, "room_members")) == group_sync.MAX_MEMBER_SYNC_PER_ROUND  # 一轮最多这么多个
    for job, _ in _commands(db, "room_members"):
        job.status = "completed"  # 手机做完了,但没拿到成员(失败/不支持)
    db.commit()
    group_sync.request_group_sync(db, channel, reason="periodic", min_gap_seconds=0)
    db.commit()
    _finish_groups_all(db, channel, [{"account_id": f"{30000000000 + n}@chatroom", "nick_name": "群"} for n in range(group_sync.MAX_MEMBER_SYNC_PER_ROUND + 4)])
    # 第二轮:前 8 个还在退避期内不重试,只补剩下的 4 个
    assert len(_commands(db, "room_members")) == group_sync.MAX_MEMBER_SYNC_PER_ROUND + 4


def test_archived_groups_are_not_asked_for_members(env) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM, management_status="archived")  # 完全关闭的群
    group_sync.request_group_sync(db, channel, reason="periodic", min_gap_seconds=0)
    db.commit()
    _finish_groups_all(db, channel, [{"account_id": OLD_ROOM, "nick_name": "已关闭的群"}])
    assert _commands(db, "room_members") == []


# ---------- 接入点 ----------

def test_the_long_poll_maintenance_requests_a_periodic_group_sync(env) -> None:
    db, channel = env
    service._last_poll_maintenance.clear()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service._poll_maintenance(db, channel)
    db.commit()
    assert len(_commands(db, "groups_all")) == 1
    assert json.loads(channel.runtime_info_json)["group_sync"]["reason"] == "periodic"


def test_ingesting_a_real_webridge_event_from_an_unknown_group_triggers_the_sync(env, woken) -> None:
    """走真实的入站事件处理(适配器解析 + channel_events 接线),而不是直接调用 observe_inbound_event。"""

    db, channel = env
    payload = {"event_name": "message", "body": {"msg_type": "text", "room_id": NEW_ROOM, "chat_id": NEW_ROOM, "is_group": True, "sender_id": "wxid_a", "content": "大家好", "message_id": "m1"}}
    ingest_inbound_event(db, channel, payload)
    db.commit()
    assert len(_commands(db, "groups_all")) == 1 and woken
    # 同一个群接着说话:防抖,不会再下发
    ingest_inbound_event(db, channel, {**payload, "body": {**payload["body"], "message_id": "m2", "content": "再说一句"}})
    db.commit()
    assert len(_commands(db, "groups_all")) == 1


def test_ingesting_a_group_member_join_event_triggers_the_sync(env, woken) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM)
    payload = {"event_name": "group_member_join", "body": {"room_id": OLD_ROOM, "member_id": "wxid_new", "member_nick_name": "新人", "is_group": True}}
    ingest_inbound_event(db, channel, payload)
    db.commit()
    assert len(_commands(db, "groups_all")) == 1
