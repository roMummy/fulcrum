"""普通消息补齐目录的独立回归:走真实入站、任务领取和回报路径。"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Chatroom, ChatroomMember
from app.services.channel_events import ingest_inbound_event
from app.services.wechat_webridge import member_directory as directory
from app.services.wechat_webridge import message_directory, service
from tests.test_webridge_member_directory import IDENTITY, ROOM, commands, emit, env, finish


def say(env, *, key="msg1", at=None, **body):
    db, channel, room, _ = env
    return ingest_inbound_event(db, channel, {"event_id": key, "event_name": "message", "body": {
        "room_id": room.external_id, "sender_id": "alice", "sender_name": "微信昵称", "content": "你好",
        "create_time": at if at is not None else now_shanghai().timestamp(), **body,
    }})


def fresh(env, *, seconds=0):
    db, _, room, _ = env
    directory.save_state(room, synced_at=(now_shanghai() - timedelta(seconds=seconds)).isoformat(), dirty=False)
    db.commit()


def test_first_unknown_group_message_creates_room_and_sender_then_requests_both_directories(env):
    db, channel, _, _ = env
    say(env, room_id="new@chatroom", conversation_title="新群", sender_id="bob", sender_name="新人")
    room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == "new@chatroom"))
    member = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id))
    assert room.name == "新群" and room.status == "pending_sync"
    assert member.member_external_id == "bob" and member.nickname == "新人" and member.last_active_at
    assert len(commands(db)) == 1
    from tests.test_webridge_group_sync import _commands
    assert len(_commands(db, "groups_all")) == 1
    finish(env, _commands(db, "groups_all")[0][0], {"groups": [{"account_id": "new@chatroom", "nick_name": "设备群名"}]})
    assert len(commands(db)) == 1  # 已有本群任务,群列表返回不再重复安排。
    finish(env, commands(db)[0], {"room_id": "new@chatroom", "members": [{"account_id": "bob", "nick_name": "真实昵称"}]})
    assert member.nickname == "真实昵称" and directory.state(room)["dirty"] is False


def test_known_group_without_complete_snapshot_refreshes_even_with_existing_members(env):
    db, _, room, member = env
    say(env)
    assert len(commands(db)) == 1 and member.last_active_at
    assert directory.state(room)["reason"] == "incomplete_directory"


def test_fresh_complete_directory_reuses_data_and_records_activity_without_queries(env):
    db, channel, room, member = env
    service.persist_directory_result(db, channel, {"type": "room_members", "params": {"room_id": ROOM}}, {
        "members": [{"account_id": "alice", "nick_name": "微信昵称"}],
    })
    db.commit()
    for number in range(3):
        say(env, key=f"fresh{number}")
    assert commands(db) == [] and member.last_active_at
    assert directory.state(room)["synced_at"]


def test_snapshot_age_is_not_extended_by_group_list_or_continuous_activity(env):
    db, channel, room, _ = env
    fresh(env, seconds=message_directory.SNAPSHOT_TTL_SECONDS + 1)
    old_sync = directory.state(room)["synced_at"]
    service.persist_directory_result(db, channel, {"type": "groups_all"}, {"groups": [{"account_id": ROOM, "nick_name": "群"}]})
    db.commit()
    say(env)
    say(env, key="again")
    assert len(commands(db)) == 1 and directory.state(room)["synced_at"] == old_sync


def test_missing_sender_triggers_refresh_with_a_fresh_snapshot(env):
    db, _, room, _ = env
    fresh(env)
    say(env, sender_id="bob", sender_name="新人")
    bob = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.member_external_id == "bob"))
    assert bob.status == "active" and len(commands(db)) == 1
    assert directory.state(room)["reason"] == "missing_sender"


@pytest.mark.parametrize("missing", ["group_name", "member_name"])
def test_missing_profile_is_refreshed_even_when_snapshot_is_fresh(env, missing):
    db, _, room, member = env
    fresh(env)
    if missing == "group_name":
        room.name = ROOM
    else:
        member.nickname = "alice"
    db.commit()
    say(env)
    assert len(commands(db)) == 1


@pytest.mark.parametrize("message_type", ["image", "voice", "video", "file"])
def test_media_message_without_text_still_refreshes_and_records_activity(env, message_type):
    db, _, _, member = env
    say(env, msg_type=message_type, content="")
    assert len(commands(db)) == 1 and member.last_active_at


def test_storage_only_and_quiet_hours_still_refresh_the_directory(env):
    db, _, room, member = env
    room.is_active = False
    room.management_status = "disabled"
    db.commit()
    result = say(env, at=now_shanghai().replace(hour=3, minute=0, second=0).timestamp())
    assert result["dispatch"]["reason"] == "quiet_hours"
    assert len(commands(db)) == 1 and member.last_active_at
    assert room.is_active is False and room.management_status == "disabled"


@pytest.mark.parametrize("status", ["archived", "left"])
def test_fully_closed_or_departed_group_is_not_reenabled_by_a_message(env, status):
    db, _, room, member = env
    if status == "archived":
        room.management_status = status
    else:
        room.status = status
    room.is_active = False
    member.status = "left"
    db.commit()
    say(env)
    assert commands(db) == [] and member.last_active_at is None and member.status == "left"
    assert room.is_active is False


@pytest.mark.parametrize("msg_type", ["system", "pat"])
def test_system_events_do_not_refresh_directory_or_count_as_speech(env, msg_type):
    db, _, _, member = env
    say(env, msg_type=msg_type, content="系统提示")
    assert member.last_active_at is None and commands(db) == []


def test_private_message_does_not_create_group_directory(env):
    db, channel, _, member = env
    ingest_inbound_event(db, channel, {"event_id": "private", "body": {
        "chat_id": "alice", "sender_id": "alice", "content": "私聊",
    }})
    assert commands(db) == [] and member.last_active_at is None


def test_duplicate_message_does_not_enqueue_again_or_advance_directory_revision(env):
    db, _, room, _ = env
    say(env, sender_id="bob")
    revision = directory.revision(room)
    result = say(env, sender_id="bob")
    assert result["deduplicated"] is True and len(commands(db)) == 1
    assert directory.revision(room) == revision


def test_old_message_cannot_undo_leave_but_new_speech_restores_membership(env):
    db, _, room, member = env
    at = now_shanghai().timestamp()
    emit(env, at=at, snapshot_after_count=0)
    say(env, key="old", at=at - 60)
    assert member.status == "left" and room.member_count == 0
    say(env, key="new", at=at + 5)
    assert member.status == "active" and room.member_count == 1
    assert directory.extra_object(member.extra_json)[directory.STATE_KEY]["membership"] == "active"
    emit(env, key="late-leave", at=at + 1, snapshot_after_count=0)
    assert member.status == "active" and room.member_count == 1
    assert len(commands(db)) == 1


def test_late_speech_does_not_move_last_active_backwards(env):
    _, _, _, member = env
    at = now_shanghai().timestamp()
    say(env, at=at)
    say(env, key="old", at=at - 60)
    assert ensure_shanghai_datetime(member.last_active_at).timestamp() >= int(at)


def test_busy_group_does_not_invalidate_snapshot_for_every_message(env):
    db, channel, room, _ = env
    say(env)
    job = commands(db)[0]
    job.available_at = now_shanghai() - timedelta(seconds=1)
    db.commit()
    with patch.object(service, "_poll_maintenance"):
        service.claim_commands(db, channel, IDENTITY)
    for number in range(4):
        say(env, key=f"busy{number}")
    finish(env, job, {"members": [{"account_id": "alice", "nick_name": "微信昵称"}]})
    assert len(commands(db)) == 1 and directory.state(room)["dirty"] is False


def test_new_sender_during_query_invalidates_old_roster_and_merges_followup(env):
    db, channel, room, _ = env
    say(env)
    job = commands(db)[0]
    job.available_at = now_shanghai() - timedelta(seconds=1)
    db.commit()
    with patch.object(service, "_poll_maintenance"):
        service.claim_commands(db, channel, IDENTITY)
    say(env, key="bob", sender_id="bob")
    finish(env, job, {"members": [{"account_id": "alice"}]})
    bob = db.scalar(select(ChatroomMember).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.member_external_id == "bob"))
    assert bob.status == "active" and len(commands(db)) == 2 and directory.state(room)["dirty"] is True


def test_repeated_missing_sender_is_throttled_after_a_successful_query(env):
    db, _, room, _ = env
    fresh(env)
    say(env, sender_id="bob")
    finish(env, commands(db)[0], {"members": [{"account_id": "alice", "nick_name": "微信昵称"}]})
    say(env, key="missing-again", sender_id="bob")
    assert len(commands(db)) == 1
    directory.save_state(room, missing_requested_at=(now_shanghai() - timedelta(seconds=11)).isoformat())
    db.commit()
    say(env, key="missing-later", sender_id="bob")
    assert len(commands(db)) == 2


def test_failure_backoff_is_not_bypassed_by_more_messages(env):
    db, _, _, _ = env
    say(env)
    finish(env, commands(db)[0], {"error": "offline"}, ok=False)
    retry = commands(db)[1]
    available = ensure_shanghai_datetime(retry.available_at)
    for number in range(3):
        say(env, key=f"after-failure{number}")
    assert len(commands(db)) == 2 and ensure_shanghai_datetime(retry.available_at) == available
    assert available > now_shanghai() + timedelta(seconds=50)
