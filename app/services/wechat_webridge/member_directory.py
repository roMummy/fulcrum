"""微桥群目录:成员事件直接落库,完整资料通过可合并的后台读取任务补齐。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, TaskJob
from app.services.chatroom_directory import get_or_create_chatroom, get_or_create_member
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import service, wakeup

DEBOUNCE_SECONDS = 2
MIN_REFRESH_SECONDS = 10
RETRY_SECONDS = 60
MAX_RETRIES_PER_POLL = 8
STATE_KEY = "member_directory"


def extra_object(value: str | None) -> dict[str, Any]:
    try:
        return service.as_object(json.loads(value or "{}"))
    except (TypeError, ValueError):
        return {}


def state(room: Chatroom) -> dict[str, Any]:
    return service.as_object(extra_object(getattr(room, "extra_json", None)).get(STATE_KEY))


def save_state(room: Chatroom, **updates: Any) -> None:
    room.extra_json = service._merge_directory_extra(getattr(room, "extra_json", None), **{STATE_KEY: {**state(room), **updates}})


def revision(room: Chatroom | None) -> int:
    return int(state(room).get("revision") or 0) if room is not None else 0


def find_room(db: Session, channel: Channel, room_id: str) -> Chatroom | None:
    if not room_id:
        return None
    # 项目会话关闭了 autoflush;重新读取锁定行前先保留本事务中的配置/状态更新。
    db.flush()
    return db.scalar(select(Chatroom).where(
        Chatroom.tenant_id == channel.tenant_id,
        Chatroom.channel_id == channel.id,
        Chatroom.external_id == room_id,
    ).with_for_update().execution_options(populate_existing=True))


def _pending(db: Session, channel: Channel, room_id: str) -> bool:
    for job in db.scalars(select(TaskJob).where(
        TaskJob.job_type == service.JOB_TYPE, TaskJob.status.in_(("pending", "running")),
    )).all():
        data = parse_job_payload(job)
        command = service.as_object(data.get("command"))
        params = service.as_object(command.get("params") or command.get("args"))
        if int(data.get("channel_id") or 0) == channel.id and command.get("type") == "room_members" and params.get("room_id") == room_id:
            return True
    return False


def request_refresh(db: Session, channel: Channel, room: Chatroom, *, reason: str) -> TaskJob | None:
    """同群待领取/执行中的读取合并;dirty 保留到成功,执行途中有新事件则结果回来后再读。"""

    if room.management_status == "archived" or room.status == "left":
        return None
    save_state(room, dirty=True)
    db.flush()
    if _pending(db, channel, room.external_id):
        return None
    now = now_shanghai()
    delay = float(DEBOUNCE_SECONDS)
    for key, interval in (("requested_at", MIN_REFRESH_SECONDS), ("failed_at", RETRY_SECONDS)):
        elapsed = service._seconds_since(state(room).get(key))
        if elapsed is not None:
            delay = max(delay, interval - elapsed)
    job = service.enqueue_command(
        db, channel, command_name="room_members", args={"room_id": room.external_id},
        priority=19, available_at=now + timedelta(seconds=delay),
    )
    save_state(room, requested_at=now.isoformat(), reason=reason)
    wakeup.notify(int(channel.id), delay + 0.1)
    return job


def retry_due(db: Session, channel: Channel) -> None:
    """失败/超时/重启后仍有未完成刷新时,随长轮询恢复;完全关闭和已退出的群除外。"""

    requested = 0
    rooms = db.scalars(select(Chatroom).where(
        Chatroom.tenant_id == channel.tenant_id, Chatroom.channel_id == channel.id,
        Chatroom.management_status != "archived", Chatroom.status != "left",
    ).order_by(Chatroom.id)).all()
    for room in rooms:
        if not state(room).get("dirty"):
            continue
        room = find_room(db, channel, room.external_id)
        if room is not None and state(room).get("dirty") and request_refresh(db, channel, room, reason="retry") is not None:
            requested += 1
            if requested >= MAX_RETRIES_PER_POLL:
                break


def complete_snapshot(result: dict[str, Any], members: list[dict[str, Any]]) -> bool:
    """空、截断、总数不符或成员 ID 无效的名单都不能用来判定谁已退群。"""

    if not members or result.get("truncated") or result.get("has_more"):
        return False
    raw = next((result[key] for key in ("members", "items") if isinstance(result.get(key), list)), [])
    if len(raw) != len(members):
        return False
    ids = {service._directory_id(item, "account_id", "user_name", "wxid", "id") for item in members}
    if "" in ids or len(ids) != len(members):
        return False
    for key in ("member_count", "total"):
        count = result.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count != len(ids):
            return False
    return True


def apply_event(db: Session, channel: Channel, normalized: dict[str, Any], conversation: Any) -> None:
    """在业务开关与免打扰判断之前处理,旧事件只存档,不回滚目录或重复发通知。"""

    from app.services.channel_events import _resolve_event_time
    from app.services.group_features.member_change import lookup_previous_names, parse_member_change

    if conversation.chat_type != "group" or not str(conversation.external_id).endswith("@chatroom"):
        return
    parsed = parse_member_change(normalized)
    if not parsed:
        return
    now = now_shanghai()
    room, _created = get_or_create_chatroom(
        db, channel, conversation.external_id, name=conversation.title or conversation.external_id,
        status="pending_sync", is_active=True, management_status="enabled", first_seen_at=now,
    )
    if room.management_status == "archived":
        return
    details = normalized.setdefault("details", {})
    previous = lookup_previous_names(db, chatroom=room, members=parsed["members"])
    event_time = _resolve_event_time(normalized)
    event_key = service.text(normalized.get("event_key"))
    changed = False
    change = parsed["change_type"]
    event_field = "nickname_event_at" if change == "rename" else "membership_event_at"
    for item in parsed["members"]:
        member = db.scalar(select(ChatroomMember).where(
            ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.chatroom_id == room.id,
            ChatroomMember.member_external_id == item["external_id"],
        ))
        old_extra = extra_object(member.extra_json) if member else {}
        last = service.as_object(old_extra.get(STATE_KEY))
        last_at = last.get(event_field)
        if last_at and event_time.timestamp() < float(last_at):
            continue
        if event_key and event_key == last.get("event_key"):
            continue
        was_active = member is not None and member.status == "active"
        if member is None:
            member, _member_created = get_or_create_member(
                db, channel, room, item["external_id"], nickname=item["display_name"], status="active",
            )
        profile = service.as_object(details.get("member_profile"))
        if profile.get("nickname"):
            member.nickname = str(profile["nickname"])[:255]
        elif not member.nickname:
            member.nickname = item["display_name"][:255]
        member_state = {**last, "event_at": max(float(last.get("event_at") or 0), event_time.timestamp()),
                        event_field: event_time.timestamp(), "event_key": event_key}
        if change == "rename":
            nick = str(parsed.get("new_name") or "")[:255]
            member_state["room_nickname"] = nick
            member.display_name = nick or member.nickname
        else:
            member.status = "active" if change == "join" else "left"
            member_state["membership"] = member.status
            if not member.display_name:
                member.display_name = item["display_name"][:255]
            if "after_count" not in parsed:
                if change == "join" and not was_active:
                    room.member_count = int(room.member_count or 0) + 1
                elif change in {"left", "removed"} and was_active:
                    room.member_count = max(0, int(room.member_count or 0) - 1)
        member.last_synced_at = now
        member.extra_json = service._merge_directory_extra(member.extra_json, **{STATE_KEY: member_state})
        changed = True
    details[STATE_KEY] = {"applied": changed, "previous": previous}
    if not changed:
        return
    room_state = state(room)
    if event_time.timestamp() >= float(room_state.get("profile_event_at") or 0):
        group = service.as_object(details.get("group"))
        name = service.text(group.get("room_name") or group.get("group_name"))
        if name:
            room.name = name[:255]
            conversation.title = room.name
        if isinstance(group.get("room_remark"), str):
            room.remark = group["room_remark"][:255] or None
        save_state(room, profile_event_at=event_time.timestamp())
    if "after_count" in parsed and event_time.timestamp() >= float(room_state.get("count_event_at") or 0):
        room.member_count = parsed["after_count"]
        save_state(room, count_event_at=event_time.timestamp())
    save_state(room, revision=revision(room) + 1, event_received_at=now.isoformat())
    db.flush()
    request_refresh(db, channel, room, reason="member_event")


def after_result(db: Session, channel: Channel, room_id: str, *, complete: bool, stale: bool = False) -> None:
    room = find_room(db, channel, room_id)
    if room is None:
        return
    if complete:
        save_state(room, dirty=False, synced_at=now_shanghai().isoformat(), failed_at=None)
    elif state(room).get("dirty"):
        if not stale:
            save_state(room, failed_at=now_shanghai().isoformat())
        request_refresh(db, channel, room, reason="newer_event" if stale else "retry")
