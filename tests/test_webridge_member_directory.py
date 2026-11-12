"""微桥成员事件与通知分离、合并刷新、乱序和旧快照保护,全部使用内存数据库。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="member-directory-logs-"))

import pytest
from sqlalchemy import select

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, TaskJob
from app.services.channel_events import ingest_inbound_event, ingest_normalized_event
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.group_features.dispatch import dispatch_group_features
from app.services.group_features.store import save_state
from app.services.wechat_webridge import member_directory as directory
from app.services.wechat_webridge import service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed

ROOM = "directory@chatroom"
IDENTITY = {"device_id": "d1", "session_id": "s1", "account_id": "a1"}


@pytest.fixture()
def env(monkeypatch):
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    room = Chatroom(tenant_id=1, bot_id=channel.bot_id, channel_id=channel.id, external_id=ROOM,
                    name="群", member_count=1, status="synced", is_active=True, management_status="enabled")
    member = ChatroomMember(tenant_id=1, bot_id=channel.bot_id, channel_id=channel.id, chatroom=room,
                            member_external_id="alice", nickname="微信昵称", display_name="旧群昵称", status="active")
    db.add_all([room, member])
    db.commit()
    monkeypatch.setattr(wakeup, "notify", lambda *args: None)
    try:
        yield db, channel, room, member
    finally:
        db.close()
        db.bind.dispose()


def emit(env, kind="group_member_leave", *, key="e1", at=None, **body):
    db, channel, room, member = env
    return ingest_inbound_event(db, channel, {"event_id": key, "event_name": kind, "body": {
        "room_id": room.external_id, "member_id": "alice", "member_nick_name": "微信昵称",
        "member_display_name": "微信昵称", "create_time": at if at is not None else now_shanghai().timestamp(),
        **body,
    }})


def commands(db):
    return [job for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all()
            if json.loads(job.payload_json)["command"]["type"] == "room_members"]


def finish(env, job, result, *, ok=True):
    db, channel, _, _ = env
    job.status = "running"
    db.commit()
    command = json.loads(job.payload_json)["command"]
    return service.accept_command_result(db, channel, {**IDENTITY, "request_id": command["request_id"], "ok": ok, "result": result})


@pytest.mark.parametrize("active,notify", [(True, False), (False, True)])
def test_leave_updates_directory_without_group_or_notification_enabled(env, active, notify):
    db, channel, room, member = env
    room.is_active = active
    room.management_status = "enabled" if active else "disabled"
    save_state(room, "member_change", enabled=notify)
    db.commit()
    result = emit(env, sender_id="alice", snapshot_after_count=0)
    assert member.status == "left" and room.member_count == 0
    assert member.last_active_at is None and member.display_name == "旧群昵称"
    assert len(commands(db)) == 1
    assert directory.state(room)["dirty"] is True
    assert not result.get("deduplicated")


def test_quiet_hours_do_not_block_directory_updates(env):
    _, _, room, member = env
    at = now_shanghai().replace(hour=3, minute=0, second=0).timestamp()
    result = emit(env, at=at, snapshot_after_count=0)
    assert result["dispatch"]["reason"] == "quiet_hours"
    assert member.status == "left" and room.member_count == 0


def test_explicit_storage_only_does_not_block_directory_updates(env):
    db, channel, room, member = env
    normalized = WechatWeBridgeAdapter().normalize_inbound_payload(channel, {"event_name": "group_member_leave", "body": {
        "room_id": ROOM, "member_id": "alice", "snapshot_after_count": 0,
    }})
    normalized["details"]["skip_plugin_dispatch"] = True
    result = ingest_normalized_event(db, channel, normalized)
    assert result["dispatch"]["reason"] == "skip_plugin_dispatch"
    assert member.status == "left" and room.member_count == 0 and len(commands(db)) == 1


def test_archived_group_still_does_not_collect_or_sync(env):
    db, _, room, member = env
    room.management_status = "archived"
    db.commit()
    result = emit(env)
    assert result["reason"] == "group_collection_off"
    assert member.status == "active" and commands(db) == []


def test_join_creates_missing_member_and_updates_group_fields(env):
    db, _, room, _ = env
    emit(env, "group_member_join", member_id="bob", member_nick_name="新人", room_name="新群名", room_remark="备注", snapshot_after_count=2)
    bob = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.member_external_id == "bob"))
    assert bob.status == "active" and bob.nickname == "新人"
    assert room.member_count == 2 and room.name == "新群名" and room.remark == "备注"


def test_unknown_group_member_event_creates_directory_without_waiting_for_phone(env):
    db, channel, room, _ = env
    emit(env, "group_member_join", room_id="new@chatroom", member_id="bob", snapshot_after_count=2)
    new_room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == "new@chatroom"))
    assert new_room is not None and new_room.member_count == 2
    assert db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == new_room.id)).member_external_id == "bob"


def test_rename_notification_keeps_old_name_and_does_not_reapply_event(env):
    db, channel, room, member = env
    save_state(room, "member_change", enabled=True)
    db.commit()
    result = emit(env, "group_member_nickname_change", new_room_nick_name="新群昵称")
    assert member.display_name == "新群昵称" and member.nickname == "微信昵称"
    conversation = db.scalar(select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_id == ROOM))
    member.display_name = "之后的昵称"
    with patch("app.services.group_features.dispatch.send_replies") as sent:
        dispatch_group_features(db, channel, result["normalized"], conversation_id=conversation.id)
    assert "旧群昵称 -> 新群昵称" in sent.call_args.args[3][0]["content"]
    assert member.display_name == "之后的昵称"


def test_duplicate_and_older_events_do_not_roll_back_member_or_count(env):
    db, _, room, member = env
    at = now_shanghai().timestamp()
    emit(env, at=at, snapshot_after_count=0)
    duplicate = emit(env, at=at, snapshot_after_count=0)
    assert duplicate["deduplicated"] is True
    stale = emit(env, "group_member_join", key="older", at=at - 60, snapshot_after_count=9)
    assert member.status == "left" and room.member_count == 0
    assert stale["normalized"]["details"][directory.STATE_KEY]["applied"] is False
    assert directory.revision(room) == 1 and len(commands(db)) == 1


def test_pending_events_coalesce_and_claim_captures_latest_revision(env):
    db, channel, room, _ = env
    emit(env)
    emit(env, "group_member_nickname_change", key="rename", new_room_nick_name="新昵称")
    assert len(commands(db)) == 1
    job = commands(db)[0]
    job.available_at = now_shanghai() - timedelta(seconds=1)
    db.commit()
    with patch.object(service, "_poll_maintenance"):
        service.claim_commands(db, channel, IDENTITY)
    assert json.loads(job.payload_json)["directory_revision"] == directory.revision(room) == 2


def test_late_leave_event_is_not_discarded_after_a_newer_nickname_event(env):
    _, _, room, member = env
    at = now_shanghai().timestamp()
    emit(env, "group_member_nickname_change", key="rename", at=at, new_room_nick_name="新昵称")
    emit(env, key="leave", at=at - 10, snapshot_after_count=0)
    assert member.status == "left" and member.display_name == "新昵称" and room.member_count == 0


def test_running_snapshot_cannot_undo_new_event_and_requests_followup(env):
    db, channel, room, member = env
    job = service.enqueue_command(db, channel, command_name="room_members", args={"room_id": ROOM})
    job.status = "running"
    db.commit()
    emit(env, snapshot_after_count=0)
    assert len(commands(db)) == 1
    finish(env, job, {"room_id": ROOM, "members": [{"account_id": "alice", "nick_name": "旧昵称"}]})
    assert member.status == "left" and member.nickname == "微信昵称" and room.member_count == 0
    assert len(commands(db)) == 2 and commands(db)[1].status == "pending"
    assert directory.state(room)["dirty"] is True


def test_complete_snapshot_updates_profile_and_preserves_explicit_cleared_nickname(env):
    db, _, room, member = env
    emit(env, "group_member_nickname_change", new_room_nick_name="")
    finish(env, commands(db)[0], {"room_id": ROOM, "room_name": "设备群名", "room_remark": "", "owner_user_name": "alice", "member_count": 1,
        "members": [{"account_id": "alice", "nick_name": "新微信昵称", "display_name": "本机备注"}]})
    assert room.name == "设备群名" and room.owner_external_id == "alice" and room.remark is None
    assert member.display_name == "新微信昵称" and member.role == "owner"
    assert directory.state(room)["dirty"] is False and directory.state(room)["synced_at"]


def test_snapshot_presence_does_not_override_explicit_leave_event(env):
    db, _, room, member = env
    emit(env, snapshot_after_count=0)
    finish(env, commands(db)[0], {"members": [{"account_id": "alice", "nick_name": "微信昵称"}]})
    assert member.status == "left" and room.member_count == 0


def test_partial_snapshot_presence_does_not_inflate_explicit_leave_count(env):
    db, _, room, member = env
    emit(env, snapshot_after_count=0)
    finish(env, commands(db)[0], {"has_more": True, "members": [{"account_id": "alice"}]})
    assert member.status == "left" and room.member_count == 0


def test_malformed_rename_does_not_clear_nickname_or_record_activity(env):
    db, _, room, member = env
    emit(env, "group_member_nickname_change", sender_id="alice")
    assert member.display_name == "旧群昵称" and member.last_active_at is None
    assert commands(db) == []


def test_timeout_and_restart_recover_dirty_refresh_without_duplicate_jobs(env):
    db, channel, room, _ = env
    emit(env)
    job = commands(db)[0]
    job.status = "running"
    job.locked_at = now_shanghai() - timedelta(seconds=service.COMMAND_RUNNING_TIMEOUT_SECONDS + 1)
    db.commit()
    assert service.reap_stale_commands(db, channel) == 1
    directory.retry_due(db, channel)
    db.commit()
    directory.retry_due(db, channel)
    assert len(commands(db)) == 2 and commands(db)[1].status == "pending"


def test_group_list_started_before_event_cannot_overwrite_new_count(env):
    db, channel, room, _ = env
    started = now_shanghai() - timedelta(seconds=1)
    emit(env, snapshot_after_count=0, room_name="事件群名", room_remark="事件备注")
    service.persist_directory_result(db, channel, {"type": "groups_all"}, {
        "groups": [{"account_id": ROOM, "nick_name": "群", "member_count": 99}],
    }, snapshot_started_at=started)
    assert room.member_count == 0 and room.name == "事件群名" and room.remark == "事件备注"


@pytest.mark.parametrize("result", [
    {"members": []},
    {"members": [{"account_id": "bob"}], "has_more": True},
    {"members": [{"account_id": "bob"}], "member_count": 8},
    {"members": [{"account_id": "bob"}], "room_id": "other@chatroom"},
    {"members": [{"account_id": "bob"}, None]},
])
def test_incomplete_snapshot_does_not_mark_other_members_left_and_retries(env, result):
    db, _, room, member = env
    emit(env, "group_member_nickname_change", new_room_nick_name="新昵称")
    finish(env, commands(db)[0], result)
    assert member.status == "active" and directory.state(room)["dirty"] is True
    assert len(commands(db)) == 2
    delay = (ensure_shanghai_datetime(commands(db)[1].available_at) - now_shanghai()).total_seconds()
    assert delay > directory.RETRY_SECONDS - 3


def test_failed_snapshot_is_retried_with_backoff_and_dirty_state_survives(env):
    db, _, room, _ = env
    emit(env)
    finish(env, commands(db)[0], {"error": "offline"}, ok=False)
    assert commands(db)[0].status == "failed" and len(commands(db)) == 2
    assert directory.state(room)["dirty"] is True
    assert ensure_shanghai_datetime(commands(db)[1].available_at) > now_shanghai() + timedelta(seconds=50)


def test_same_member_id_in_another_channel_is_not_modified(env):
    db, channel, room, _ = env
    other = Channel(tenant_id=1, bot_id=channel.bot_id, name="另一个账号", channel_type="wechat_webridge")
    db.add(other)
    db.flush()
    other_room = Chatroom(tenant_id=1, bot_id=channel.bot_id, channel_id=other.id, external_id=ROOM, name="另一个群")
    db.add(other_room)
    db.flush()
    other_member = ChatroomMember(tenant_id=1, bot_id=channel.bot_id, channel_id=other.id, chatroom_id=other_room.id,
                                  member_external_id="alice", status="active")
    db.add(other_member)
    db.commit()
    emit(env)
    assert other_member.status == "active" and directory.state(other_room) == {}
