"""WeBridge 渠道后台的本地观测视图。

这里优先展示服务端已经确认过的联系人、群组和消息；主动刷新好友或群成员仍通过
能力目录里的通用命令桥执行，避免把未确认的手机缓存伪装成本地事实。
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Chatroom, ChatroomMember, Channel, Conversation, Message, TaskJob
from app.services.task_queue import parse_job_payload


def build_webridge_dashboard(db: Session, channel: Channel, *, message_limit: int = 40) -> dict:
    """构建单个 WeBridge 渠道的好友、群组和消息总览。"""

    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.channel_id == channel.id)
        .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.id.desc())
    ).all()
    direct = [item for item in conversations if str(item.chat_type or "direct") == "direct"]
    groups = [item for item in conversations if str(item.chat_type or "") == "group"]
    chatrooms = db.scalars(select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.id.desc())).all()
    members_by_chatroom = {
        int(chatroom.id): db.scalars(
            select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id).order_by(ChatroomMember.id.asc())
        ).all()
        for chatroom in chatrooms
    }
    messages = db.scalars(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.channel_id == channel.id)
        .order_by(Message.id.desc())
        .limit(max(1, min(int(message_limit or 40), 100)))
    ).all()
    conversation_map = {int(item.id): item for item in conversations}
    command_contacts, command_groups, command_members, sync_history = _build_command_snapshots(db, channel)
    return {
        "direct": direct,
        "groups": groups,
        "chatrooms": chatrooms,
        "members_by_chatroom": members_by_chatroom,
        "messages": messages,
        "conversation_map": conversation_map,
        "command_contacts": command_contacts,
        "command_groups": command_groups,
        "command_members": command_members,
        "sync_history": sync_history,
        "counts": {
            "conversations": len(conversations),
            "direct": len(direct),
            "groups": len(groups),
            "chatrooms": len(chatrooms),
            "messages": int(
                db.scalar(
                    select(func.count(Message.id))
                    .join(Conversation, Message.conversation_id == Conversation.id)
                    .where(Conversation.channel_id == channel.id)
                )
                or 0
            ),
        },
    }


def _build_command_snapshots(db: Session, channel: Channel) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """从已完成的 friends/groups/room_members 命令结果提取可展示资料。"""

    contacts: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    members: dict[str, dict] = {}
    history: list[dict] = []
    jobs = db.scalars(
        select(TaskJob)
        .where(TaskJob.job_type == "wechat_webridge_outbound", TaskJob.tenant_id == channel.tenant_id)
        .order_by(TaskJob.id.desc())
        .limit(100)
    ).all()
    for job in jobs:
        payload = parse_job_payload(job)
        if int(payload.get("channel_id") or 0) != int(channel.id):
            continue
        command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
        command_type = str(command.get("type") or "").strip()
        if command_type not in {"friends", "friends_all", "groups_all", "room_members"}:
            continue
        try:
            result_wrapper = json.loads(job.result_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result_wrapper = {}
        result = result_wrapper.get("webridge", {}).get("result", {}) if isinstance(result_wrapper, dict) else {}
        if not isinstance(result, dict):
            result = {}
        items = result.get("items") if isinstance(result.get("items"), list) else []
        if command_type == "groups_all":
            items = result.get("groups") if isinstance(result.get("groups"), list) else items
        if command_type == "room_members":
            items = result.get("members") if isinstance(result.get("members"), list) else items
        for item in items:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("account_id") or item.get("user_name") or item.get("wxid") or item.get("room_id") or item.get("id") or "").strip()
            if not identifier:
                continue
            normalized = {
                **item,
                "external_id": identifier,
                "display_name": item.get("display_name") or item.get("remark") or item.get("nickname") or item.get("name") or identifier,
                "avatar_url": item.get("avatar_url") or item.get("avatar") or item.get("head_img_url") or "",
                "last_synced_at": job.finished_at or job.created_at,
            }
            if command_type == "room_members":
                members[identifier] = normalized
            elif command_type == "groups_all":
                groups[identifier] = normalized
            else:
                contacts[identifier] = normalized
        history.append({"command_type": command_type, "status": job.status, "finished_at": job.finished_at or job.created_at})
    return list(contacts.values()), list(groups.values()), list(members.values()), history[:12]
