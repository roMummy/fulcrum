"""消息中心的群成员 @ 候选与服务端校验，共用同一份成员范围。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, Message, WorkWechatContact
from app.services.identity_directory import _avatar_from_extra, is_system_sender, looks_like_raw_id, make_identity
from app.services.message_actions import capabilities, is_own_message, object_json


ALL_ID = "notify@all"


def _supported(channel: Channel | None, conversation: Conversation) -> bool:
    return bool(channel is not None and conversation.channel_id == channel.id
                and conversation.bot_id == channel.bot_id and conversation.tenant_id == channel.tenant_id
                and conversation.chat_type == "group" and capabilities(channel, conversation)["mention"])


def can_mention_all(db: Session, channel: Channel | None, conversation: Conversation) -> bool:
    """仅以当前渠道登录账号和本群已保存的群主/管理员资料确认全体 @ 权限。"""

    if not _supported(channel, conversation) or channel.channel_type != "wechat_webridge":
        return False
    bridge = object_json(channel.runtime_info_json).get("webridge")
    own_id = str(bridge.get("account_id") or "").strip() if isinstance(bridge, dict) else ""
    if not own_id or own_id == ALL_ID or is_system_sender(own_id):
        return False
    room = db.scalar(select(Chatroom).where(
        Chatroom.tenant_id == channel.tenant_id, Chatroom.bot_id == channel.bot_id,
        Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id,
    ).order_by(Chatroom.id.desc()).limit(1))
    if room is None or room.status == "left":
        return False
    member = db.scalar(select(ChatroomMember).where(
        ChatroomMember.tenant_id == channel.tenant_id, ChatroomMember.bot_id == channel.bot_id,
        ChatroomMember.channel_id == channel.id, ChatroomMember.chatroom_id == room.id,
        ChatroomMember.member_external_id == own_id,
    ).order_by(ChatroomMember.id.desc()).limit(1))
    # 已离群的本人记录优先于可能尚未刷新的群主字段，不能借旧群主资料继续发全体通知。
    if member is not None and member.status == "left":
        return False
    return str(room.owner_external_id or "").strip() == own_id or bool(member is not None and member.role in {"owner", "admin"})


def _name(external_id: str, *values: str | None) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and text != external_id and not looks_like_raw_id(text) and not is_system_sender(text):
            return text
    return "群成员"


def _candidates(db: Session, channel: Channel, conversation: Conversation) -> list[dict]:
    if not _supported(channel, conversation):
        return []
    rows = db.scalars(select(ChatroomMember).join(Chatroom, Chatroom.id == ChatroomMember.chatroom_id).where(
        Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id,
        ChatroomMember.channel_id == channel.id, ChatroomMember.tenant_id == channel.tenant_id,
    ).order_by(ChatroomMember.id.desc())).all()
    entries = {}
    if rows:
        # 有成员目录时它才是成员范围；已离群记录不能再被旧发言记录补回。
        seen = set()
        for row in rows:
            external_id = str(row.member_external_id or "").strip()
            if external_id in seen:
                continue
            seen.add(external_id)
            if row.status != "left":
                entries[external_id] = (row.display_name, row.nickname, _avatar_from_extra(row.extra_json))
    else:
        # 老群尚无成员快照时，兼容本会话已知发言人；每人只读取最近一条消息的昵称。
        latest = select(func.max(Message.id)).where(
            Message.conversation_id == conversation.id, Message.status != "echo", Message.sender_id.is_not(None),
        ).group_by(Message.sender_id)
        for row in db.scalars(select(Message).where(Message.id.in_(latest)).order_by(Message.id.desc())).all():
            if not is_own_message(row, channel):
                entries[str(row.sender_id or "").strip()] = (row.sender_name, "", "")
    entries = {external_id: value for external_id, value in entries.items()
               if external_id and external_id != ALL_ID and external_id != conversation.external_id
               and not is_system_sender(external_id)
               and not is_own_message(Message(direction="in", sender_id=external_id), channel)}
    contacts = {}
    if entries:
        for row in db.scalars(select(WorkWechatContact).where(
            WorkWechatContact.channel_id == channel.id, WorkWechatContact.deleted_at.is_(None),
            WorkWechatContact.contact_external_id.in_(entries),
        ).order_by(WorkWechatContact.id.desc())).all():
            contacts.setdefault(row.contact_external_id, row)
    result = []
    for external_id, (group_name, nickname, avatar) in entries.items():
        contact = contacts.get(external_id)
        remark, contact_name = (contact.display_name, contact.name) if contact is not None else ("", "")
        identity = make_identity(external_id, _name(external_id, group_name, nickname, remark, contact_name),
                                 avatar or (contact.avatar_url if contact is not None else ""))
        result.append({"id": external_id, "name": identity["name"], "avatar_url": identity["avatar_url"],
                       "initial": identity["initial"],
                       "_search": "\n".join(str(value or "") for value in (external_id, group_name, nickname, remark, contact_name)).casefold()})
    result.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    if can_mention_all(db, channel, conversation):
        # 微桥 v8.0.80 长轮询协议明确使用 notify@all，不能展开成全部成员 wxid。
        result.insert(0, {"id": ALL_ID, "name": "所有人", "avatar_url": "", "initial": "全", "_search": "所有人 全体成员 all"})
    return result


def members(db: Session, channel: Channel, conversation: Conversation, *, query: str = "", limit: int = 30) -> dict:
    """按群昵称、微信昵称或好友备注搜索群成员，并限制返回数量。"""

    keyword = str(query or "").strip().lstrip("@").casefold()
    limit = max(1, min(int(limit), 100))
    rows = [item for item in _candidates(db, channel, conversation) if not keyword or keyword in item["_search"]]
    return {"members": [{key: value for key, value in item.items() if key != "_search"} for item in rows[:limit]],
            "has_more": len(rows) > limit}


def validate_members(db: Session, channel: Channel, conversation: Conversation, ids: list[str]) -> list[dict]:
    """按相同群成员范围校验选择，保持选择顺序并移除重复项。"""

    if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(value, str) or not value.strip() or len(value) > 128 for value in ids):
        raise ValueError("@ 成员参数无效")
    if not ids:
        return []
    selected = list(dict.fromkeys(value.strip() for value in ids))
    if ALL_ID in selected and not can_mention_all(db, channel, conversation):
        raise ValueError("只有群主或管理员才能 @所有人")
    if not _supported(channel, conversation):
        raise ValueError("当前渠道不支持群聊 @")
    available = {item["id"]: item for item in _candidates(db, channel, conversation)}
    if any(external_id not in available for external_id in selected):
        raise ValueError("所选成员不属于当前群聊或已离群")
    return [{"id": external_id, "name": available[external_id]["name"]} for external_id in selected]
