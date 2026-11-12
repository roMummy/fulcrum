"""普通群消息触发目录补齐;完整快照的时效与最后发言时间分开记录。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember
from app.services.wechat_webridge import member_directory as directory
from app.services.wechat_webridge import service

# 与 Koishi 的微桥目录一致:有消息时检查五分钟时效,并非每五分钟轮询所有群。
SNAPSHOT_TTL_SECONDS = 300
MISSING_RETRY_SECONDS = 10
NON_SPEECH_TYPES = {"system", "pat", "friend_request", "contact", "moments", "favorite", "official_account"}


def is_group_message(normalized: dict[str, Any], conversation: Any) -> bool:
    message_type = service.text(service.as_object(normalized.get("message")).get("message_type"))
    return (
        service.text(normalized.get("event_type")) == "message"
        and conversation.chat_type == "group"
        and str(conversation.external_id).endswith("@chatroom")
        and message_type not in NON_SPEECH_TYPES
        and not message_type.startswith("group_member_")
        and not isinstance(service.as_object(normalized.get("details")).get(directory.STATE_KEY), dict)
    )


def observe_message(db: Session, channel: Channel, normalized: dict[str, Any], conversation: Any) -> None:
    """入站去重后执行:先补发言人记录,按目录时效安排异步查询,不等待手机返回。"""

    if not is_group_message(normalized, conversation):
        return
    from app.services.channel_events import _resolve_event_time

    room = directory.find_room(db, channel, conversation.external_id)
    now = now_shanghai()
    if room is None:
        room = Chatroom(
            tenant_id=channel.tenant_id, bot_id=channel.bot_id, channel_id=channel.id,
            external_id=conversation.external_id, name=conversation.title or conversation.external_id,
            status="pending_sync", is_active=True, management_status="enabled", first_seen_at=now,
        )
        db.add(room)
        db.flush()
    if room.management_status == "archived" or room.status == "left":
        return
    room.last_seen_at = now
    message = service.as_object(normalized.get("message"))
    sender_id = service.text(message.get("sender_id"))
    missing_sender = False
    membership_changed = False
    if sender_id and sender_id != room.external_id:
        member = db.scalar(select(ChatroomMember).where(
            ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.chatroom_id == room.id,
            ChatroomMember.member_external_id == sender_id,
        ))
        missing_sender = member is None
        if member is None:
            member = ChatroomMember(
                tenant_id=channel.tenant_id, bot_id=channel.bot_id, channel_id=channel.id,
                chatroom_id=room.id, member_external_id=sender_id, status="active",
            )
            db.add(member)
            membership_changed = True
        member_state = service.as_object(directory.extra_object(member.extra_json).get(directory.STATE_KEY))
        missing_sender = missing_sender or member.status != "active" or bool(member_state.get("observed_only")) or not member.nickname or member.nickname == sender_id
        event_time = _resolve_event_time(normalized)
        # 延迟到达的旧发言可以补历史活跃时间,不能撤销更新的退群事件。
        if event_time.timestamp() >= float(member_state.get("membership_event_at") or 0):
            if member.status != "active":
                member.status = "active"
                room.member_count = int(room.member_count or 0) + 1
                membership_changed = True
            if missing_sender and not member.last_synced_at:
                member_state["observed_only"] = True
            member_state.update(membership="active", membership_event_at=event_time.timestamp())
            if not member.nickname or member.nickname == sender_id:
                member.nickname = (service.text(message.get("sender_name")) or sender_id)[:255]
            if not member.display_name:
                member.display_name = member.nickname
            room.member_count = max(int(room.member_count or 0), 1)
            member.extra_json = service._merge_directory_extra(member.extra_json, **{directory.STATE_KEY: member_state})
        if member.last_active_at is None or event_time > ensure_shanghai_datetime(member.last_active_at):
            member.last_active_at = event_time
    if membership_changed:
        directory.save_state(room, revision=directory.revision(room) + 1, event_received_at=now.isoformat())

    age = service._seconds_since(directory.state(room).get("synced_at"))
    missing_profile = not room.name or room.name == room.external_id
    if directory.state(room).get("dirty"):
        reason = "unfinished_directory"
    elif age is None:
        reason = "incomplete_directory"
    elif age >= SNAPSHOT_TTL_SECONDS:
        reason = "stale_directory"
    elif missing_sender or missing_profile:
        since = service._seconds_since(directory.state(room).get("missing_requested_at"))
        if since is not None and since < MISSING_RETRY_SECONDS:
            return
        reason = "missing_sender" if missing_sender else "missing_group_profile"
    else:
        return
    if missing_sender or missing_profile:
        directory.save_state(room, missing_requested_at=now.isoformat())
    directory.request_refresh(db, channel, room, reason=reason)
