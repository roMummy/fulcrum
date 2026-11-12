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

def _finish_groups_all(db, channel, items, **result_extra):
    job, command = _commands(db, "groups_all")[-1]
    job.status = "running"
    db.commit()
    result = {"ok": True, "total": len(items), "items": items, **result_extra}
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service.accept_command_result(db, channel, {**IDENTITY, "request_id": command["request_id"], "ok": True, "result": result})
    db.commit()


def _snapshot(db, channel, items, **result_extra):
    """模拟一次完整的群列表同步:下发 groups_all,手机回报 items。"""

    runtime = json.loads(channel.runtime_info_json)
    runtime.get("group_sync", {}).pop("requested_at", None)  # 清掉防抖,每次都能下发
    channel.runtime_info_json = json.dumps(runtime)
    group_sync.request_group_sync(db, channel, reason="periodic", min_gap_seconds=0)
    db.commit()
    _finish_groups_all(db, channel, items, **result_extra)


def _row(db, external_id):
    db.expire_all()
    return db.scalar(select(Chatroom).where(Chatroom.external_id == external_id))


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


def test_directory_sync_preserves_group_settings_and_local_member_metadata(env) -> None:
    db, channel = env
    room = _add_room(db, channel, OLD_ROOM, members=1)
    settings = {"member_change": {"enabled": True, "config": {"notify_join": False}}}
    room.extra_json = json.dumps({"group_features": settings, "auto_download_media": True})
    member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id))
    member.extra_json = json.dumps({"local_note": "keep"})
    service.persist_directory_result(db, channel, {"type": "groups_all"}, {"groups": [
        {"account_id": OLD_ROOM, "nick_name": "新群名", "avatar_url": "group.png"},
    ]})
    extra = json.loads(room.extra_json)
    assert extra["group_features"] == settings and extra["auto_download_media"] is True
    assert extra["avatar_url"] == "group.png" and room.name == "新群名"
    service.persist_directory_result(db, channel, {"type": "room_members", "params": {"room_id": OLD_ROOM}}, {
        "members": [{"account_id": member.member_external_id, "nick_name": "更新昵称", "avatar_url": "member.png"}],
    })
    extra = json.loads(member.extra_json)
    assert extra["local_note"] == "keep" and extra["avatar_url"] == "member.png"
    assert member.nickname == "更新昵称"


@pytest.mark.parametrize("extra", ["broken", "[]", "null"])
def test_directory_sync_recovers_invalid_extra_json(env, extra) -> None:
    db, channel = env
    room = _add_room(db, channel, OLD_ROOM)
    room.extra_json = extra
    service.persist_directory_result(db, channel, {"type": "groups_all"}, {
        "groups": [{"account_id": OLD_ROOM, "nick_name": "群"}],
    })
    assert json.loads(room.extra_json)["source"] == "wechat_webridge"


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


# ---------- 退群 / 被移出 ----------

OTHER_ROOM = "10000000009@chatroom"


def test_a_group_missing_from_two_consecutive_snapshots_is_marked_as_left(env) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM, members=2)
    _add_room(db, channel, OTHER_ROOM, members=2)
    _snapshot(db, channel, [{"account_id": OTHER_ROOM, "nick_name": "还在的群"}])
    assert _row(db, OLD_ROOM).status == "synced"  # 只缺失一次:可能是手机偶发返回异常,先不认定
    _snapshot(db, channel, [{"account_id": OTHER_ROOM, "nick_name": "还在的群"}])
    left, kept = _row(db, OLD_ROOM), _row(db, OTHER_ROOM)
    assert (left.status, left.is_active, left.management_status) == ("left", False, "enabled")  # 运行模式保持用户原来的选择
    assert json.loads(left.extra_json)["left"]["was_active"] is True
    assert (kept.status, kept.is_active) == ("synced", True)
    assert len(_row(db, OLD_ROOM) and db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == left.id)).all()) == 2  # 成员记录保留


def test_seeing_the_group_again_resets_the_miss_counter(env) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM)
    other = [{"account_id": OTHER_ROOM, "nick_name": "别的群"}]
    _snapshot(db, channel, other)
    _snapshot(db, channel, other + [{"account_id": OLD_ROOM, "nick_name": "老群"}])  # 又出现了
    _snapshot(db, channel, other)
    assert _row(db, OLD_ROOM).status == "synced"  # 不是"连续"缺失


def test_a_removed_message_makes_one_missing_snapshot_enough(env, woken) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM)
    group_sync.observe_inbound_event(db, channel, _event(message_type="system", content='你被"小明"移出群聊'), _group(OLD_ROOM))
    db.commit()
    assert len(_commands(db, "groups_all")) == 1 and json.loads(channel.runtime_info_json)["group_sync"]["reason"] == "leave_system_message"
    job, _ = _commands(db, "groups_all")[0]
    _finish_groups_all(db, channel, [{"account_id": OTHER_ROOM, "nick_name": "别的群"}])
    assert _row(db, OLD_ROOM).status == "left"


@pytest.mark.parametrize("content", ["你已退出群聊", '"小明"将你移出了群聊', "该群聊已被解散", "你已被移出群聊"])
def test_leave_system_messages_are_recognized(env, woken, content) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM)
    group_sync.observe_inbound_event(db, channel, _event(message_type="system", content=content), _group(OLD_ROOM))
    assert OLD_ROOM in json.loads(channel.runtime_info_json)["group_sync"]["suspect_left"]


def test_partial_snapshots_never_mark_a_group_as_left(env) -> None:
    db, channel = env
    _add_room(db, channel, OLD_ROOM)
    other = [{"account_id": OTHER_ROOM, "nick_name": "别的群"}]
    for extra in ({"total": 99}, {"has_more": True}, {"truncated": True}):  # 条数对不上 / 还有更多 / 被截断
        _snapshot(db, channel, other, **extra)
        _snapshot(db, channel, other, **extra)
    assert _row(db, OLD_ROOM).status == "synced"
    assert group_sync.complete_snapshot_ids({"params": {"keyword": "测试"}}, {"items": other}) is None  # 带关键词过滤的只是一部分
    assert group_sync.complete_snapshot_ids({"params": {}}, {"items": other}) == {OTHER_ROOM}


def test_groups_that_were_never_synced_are_not_marked_as_left(env) -> None:
    db, channel = env
    room = _add_room(db, channel, OLD_ROOM)
    room.status = "pending_sync"  # 手动登记的群,手机列表里从来没出现过
    db.commit()
    other = [{"account_id": OTHER_ROOM, "nick_name": "别的群"}]
    _snapshot(db, channel, other)
    _snapshot(db, channel, other)
    assert _row(db, OLD_ROOM).status == "pending_sync"


@pytest.mark.parametrize("was_active, mode", [(True, "enabled"), (False, "disabled")])
def test_a_group_that_comes_back_gets_its_previous_state_restored(env, was_active, mode) -> None:
    db, channel = env
    room = _add_room(db, channel, OLD_ROOM, members=1, management_status=mode)
    room.is_active = was_active
    db.commit()
    other = [{"account_id": OTHER_ROOM, "nick_name": "别的群"}]
    _snapshot(db, channel, other)
    _snapshot(db, channel, other)
    assert _row(db, OLD_ROOM).status == "left"
    _snapshot(db, channel, other + [{"account_id": OLD_ROOM, "nick_name": "老群", "member_count": 5}])  # 又被拉回来了
    back = _row(db, OLD_ROOM)
    assert (back.status, back.is_active, back.management_status, back.member_count) == ("synced", was_active, mode, 5)


def test_left_groups_are_not_asked_for_members(env) -> None:
    db, channel = env
    room = _add_room(db, channel, OLD_ROOM)  # 没有任何成员记录
    room.status = "left"
    db.commit()
    group_sync.request_group_sync(db, channel, reason="periodic", min_gap_seconds=0)
    db.commit()
    _finish_groups_all(db, channel, [{"account_id": OTHER_ROOM, "nick_name": "别的群"}])
    assert OLD_ROOM not in [command["params"]["room_id"] for _, command in _commands(db, "room_members")]


def test_sending_to_a_group_the_bot_has_left_is_refused_with_a_clear_reason(env) -> None:
    from app.models.entities import Conversation
    from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter

    db, channel = env
    room = _add_room(db, channel, OLD_ROOM)
    conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id=OLD_ROOM, chat_type="group")
    db.add(conversation)
    db.commit()
    payload = {"message_type": "text", "content": "你好"}
    service.enqueue_message_command(db, channel, conversation, payload)  # 还在群里:正常入队
    room.status = "left"
    db.commit()
    with pytest.raises(ValueError, match="已不在这个群里"):
        service.enqueue_message_command(db, channel, conversation, payload)
    result = WechatWeBridgeAdapter().send_message(channel, conversation, payload)  # 经适配器:记为失败并带原因,不会下发命令
    assert result["ok"] is False and "已不在这个群里" in str(result)
    assert len([1 for _ in _commands(db, "send_message")]) == 1
