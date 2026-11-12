"""把微信 iOS 收到的群消息落成可管理的群实体。

手机端没有“同步群列表”的接口：``Friend/GetList`` 只返回保存到通讯录的群，
未保存的群只能靠消息发现。所以群管理页要显示群，只能在第一条群消息进来时
创建 ``Chatroom``；成员则复用昵称解析阶段已经缓存的群名册，不再额外打手机。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation
from app.services.chatroom_registry import mark_chatroom_discovered
from app.services.wechat_gugatool_directory import (
    cache_group,
    cached_group,
    finish_group_refresh,
    save_account_profile,
)


logger = get_logger(__name__)

# 首次建群时把名册整体落库；超大的群截断，避免一次事务写太多行。
MAX_ROSTER_MEMBERS = 500


def sync_wechat_gugatool_chatroom_from_message(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    conversation: Conversation | None = None,
) -> dict[str, Any]:
    """按一条群消息创建/更新群与成员，返回本次落库的摘要。"""

    conversation_payload = normalized.get("conversation") or {}
    message_payload = normalized.get("message") or {}
    details = normalized.get("details") or {}

    external_id = str(conversation_payload.get("external_id") or "").strip()
    if str(conversation_payload.get("chat_type") or "") != "group" or not external_id.endswith("@chatroom"):
        return {"chatroom_created": False, "member_created": 0, "chatroom_id": 0}

    seen_at = _resolve_seen_at(details)
    roster = cached_group(int(channel.id), external_id) or {}
    roster_members = roster.get("members") if isinstance(roster.get("members"), dict) else {}

    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == external_id)
    )
    created = False
    if chatroom is None:
        chatroom = Chatroom(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            external_id=external_id,
            status="message_observed",
            name=_resolve_chatroom_name(roster, conversation_payload, conversation, external_id),
        )
        db.add(chatroom)
        db.flush()
        created = True

    # 新建的群、或还没落过名册的群，整体写一次成员；之后每条消息只更新发送者。
    previous_extra = _parse_json(chatroom.extra_json)
    needs_roster = created or not previous_extra.get("roster_synced")

    mark_chatroom_discovered(
        db,
        channel=channel,
        chatroom=chatroom,
        seen_at=seen_at,
        registration_source="passive",
        # 与 iPad 渠道一致：自动发现的群默认纳入管理，之后后台停用不会被重新打开。
        enable_management=created,
    )

    name = _resolve_chatroom_name(roster, conversation_payload, conversation, external_id)
    if name:
        chatroom.name = name
    chatroom.status = "active"
    chatroom.last_synced_at = seen_at
    chatroom.extra_json = json.dumps(
        {
            "source": "wechat_gugatool_message_ingest",
            "name_source": "group_roster" if roster.get("title") else "message_payload",
            "member_count": len(roster_members) or previous_extra.get("member_count"),
            # 标记名册已经整体落过库，避免每条群消息都重复遍历成员。
            "roster_synced": bool(previous_extra.get("roster_synced")) or bool(needs_roster and roster_members),
            "last_event_key": normalized.get("event_key"),
            "last_sender_id": message_payload.get("sender_id"),
            "last_message_type": message_payload.get("message_type"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    member_created = 0
    if needs_roster and roster_members:
        # 首次拿到群资料时把整份名册写进来，群管理页立刻有成员列表。
        for wxid, profile in list(roster_members.items())[:MAX_ROSTER_MEMBERS]:
            if _upsert_member(db, channel=channel, chatroom=chatroom, profile=profile, seen_at=seen_at):
                member_created += 1
    else:
        sender_id = str(message_payload.get("sender_id") or "").strip()
        profile = details.get("member_profile") if isinstance(details.get("member_profile"), dict) else None
        if sender_id:
            if _upsert_member(
                db,
                channel=channel,
                chatroom=chatroom,
                profile=profile or {"wxid": sender_id, "name": message_payload.get("sender_name")},
                seen_at=seen_at,
            ):
                member_created += 1

    logger.info(
        "wechat ios chatroom materialized",
        extra={
            "channel_id": channel.id,
            "chatroom_id": chatroom.id,
            "chatroom_created": created,
            "members_created": member_created,
            "roster_size": len(roster_members),
        },
    )
    return {"chatroom_created": created, "member_created": member_created, "chatroom_id": int(chatroom.id)}


def apply_wechat_gugatool_roster_result(
    db: Session,
    channel: Channel,
    *,
    room_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """消费手机 get_chatroom_info 的结果,补群名、成员和当前账号资料。"""

    payload = result.get("Data") if isinstance(result.get("Data"), dict) else result
    if not isinstance(payload, dict) or not payload:
        finish_group_refresh(int(channel.id), room_id)
        return {"updated": False}
    room = str(room_id or payload.get("ChatRoomWxid") or "").strip()
    if not room:
        finish_group_refresh(int(channel.id), room_id)
        return {"updated": False}

    title = str(payload.get("Name") or payload.get("DisplayName") or "").strip()
    members: dict[str, dict[str, Any]] = {}
    raw_members = payload.get("Members")
    if isinstance(raw_members, list):
        for item in raw_members:
            if not isinstance(item, dict):
                continue
            wxid = str(item.get("Wxid") or "").strip()
            if not wxid:
                continue
            members[wxid] = {
                "wxid": wxid,
                "name": str(item.get("Name") or item.get("NickName") or "").strip(),
                "nickname": str(item.get("NickName") or "").strip(),
                "display_name": str(item.get("DisplayName") or item.get("RoomNickName") or "").strip(),
                "remark": str(item.get("Remark") or "").strip(),
                "alias": str(item.get("Alias") or "").strip(),
                "avatar_url": str(item.get("HeadImgUrl") or item.get("BigHeadImgUrl") or "").strip(),
                "is_owner": bool(item.get("IsOwner")),
                "is_admin": bool(item.get("IsAdmin")),
            }
    cache_group(int(channel.id), room, title=title, members=members)
    finish_group_refresh(int(channel.id), room)

    seen_at = now_shanghai()
    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == room)
    )
    created = False
    if chatroom is None:
        chatroom = Chatroom(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            external_id=room,
            status="message_observed",
            name=title or room,
        )
        db.add(chatroom)
        db.flush()
        created = True
    if title:
        chatroom.name = title[:255]
    chatroom.last_synced_at = seen_at
    previous_extra = _parse_json(chatroom.extra_json)
    chatroom.extra_json = json.dumps(
        {
            **previous_extra,
            "source": "wechat_gugatool_group_roster",
            "name_source": "group_roster",
            "member_count": len(members) or previous_extra.get("member_count"),
            "roster_synced": bool(members),
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    member_created = 0
    for wxid, profile in list(members.items())[:MAX_ROSTER_MEMBERS]:
        if _upsert_member(db, channel=channel, chatroom=chatroom, profile=profile, seen_at=seen_at):
            member_created += 1

    conversation = db.scalar(
        select(Conversation).where(Conversation.channel_id == channel.id, Conversation.external_id == room)
    )
    if conversation is not None and title:
        conversation.title = title[:255]

    # 当前登录账号也在名册里:顺手回填账号昵称/头像,列表不再只显示 wxid。
    runtime = _parse_json(channel.runtime_info_json)
    connection = runtime.get("account_connection") if isinstance(runtime.get("account_connection"), dict) else {}
    account_id = str(connection.get("account_id") or "").strip()
    self_profile = members.get(account_id) if account_id else None
    if self_profile:
        save_account_profile(
            channel,
            account_id=account_id,
            # 账号全局昵称优先于群昵称，列表头像旁的名称才稳定。
            name=str(self_profile.get("nickname") or self_profile.get("display_name") or self_profile.get("name") or "").strip(),
            avatar_url=str(self_profile.get("avatar_url") or "").strip(),
        )

    logger.info(
        "wechat ios roster applied",
        extra={"channel_id": channel.id, "chatroom_id": chatroom.id, "chatroom_created": created, "members_created": member_created, "roster_size": len(members)},
    )
    return {"updated": True, "chatroom_created": created, "member_created": member_created, "chatroom_id": int(chatroom.id)}


def build_wechat_gugatool_chatroom_detail(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    """群详情页：复用通用构造，只改掉不适用于 iOS 的同步入口和提示。"""

    from app.services.workwechat_chatrooms import build_workwechat_chatroom_detail

    detail = build_workwechat_chatroom_detail(db, channel, chatroom)
    detail.update(
        {
            # 手机端没有“同步群成员”的命令通道，成员靠收到群消息时自动刷新。
            "supports_sync": False,
            "sync_button_label": "",
            "sync_action": "",
            "member_section_hint": "成员来自手机端群资料接口，收到该群消息时自动同步；名册只在首次建群时整体拉取。",
        }
    )
    return detail


def _upsert_member(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    profile: dict[str, Any],
    seen_at: datetime,
) -> bool:
    """按 wxid 幂等写入成员，返回是否新建。"""

    member_id = str(profile.get("wxid") or "").strip()
    if not member_id:
        return False
    member = db.scalar(
        select(ChatroomMember).where(
            ChatroomMember.chatroom_id == chatroom.id,
            ChatroomMember.member_external_id == member_id,
        )
    )
    created = False
    if member is None:
        member = ChatroomMember(
            chatroom_id=chatroom.id,
            bot_id=channel.bot_id,
            channel_id=channel.id,
            member_external_id=member_id,
            status="active",
        )
        db.add(member)
        created = True

    nickname = str(profile.get("name") or profile.get("nickname") or "").strip()
    display_name = str(profile.get("display_name") or profile.get("nickname") or "").strip()
    if nickname:
        member.nickname = nickname[:255]
    if display_name:
        member.display_name = display_name[:255]
    if profile.get("is_owner"):
        member.role = "owner"
    elif profile.get("is_admin"):
        member.role = "admin"
    member.status = "active"
    member.last_synced_at = seen_at
    member.last_active_at = seen_at
    member.extra_json = json.dumps(
        {
            "source": "wechat_gugatool_group_roster",
            "alias": str(profile.get("alias") or "") or None,
            "remark": str(profile.get("remark") or "") or None,
            "avatar_url": str(profile.get("avatar_url") or "") or None,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return created


def _resolve_chatroom_name(
    roster: dict[str, Any],
    conversation_payload: dict[str, Any],
    conversation: Conversation | None,
    external_id: str,
) -> str:
    """群名优先级：手机端群资料 > 消息里的群名 > 已有会话标题 > 群 id。"""

    candidates = [
        str(roster.get("title") or "").strip(),
        str(conversation_payload.get("title") or "").strip(),
        str(getattr(conversation, "title", "") or "").strip(),
    ]
    for candidate in candidates:
        if candidate and candidate != external_id:
            return candidate[:255]
    return external_id


def _parse_json(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_seen_at(details: dict[str, Any]) -> datetime:
    raw = details.get("create_time") if isinstance(details, dict) else None
    try:
        timestamp = int(raw or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if timestamp <= 0:
        return now_shanghai()
    try:
        return ensure_shanghai_datetime(datetime.fromtimestamp(timestamp))
    except (OverflowError, OSError, ValueError):
        return now_shanghai()
