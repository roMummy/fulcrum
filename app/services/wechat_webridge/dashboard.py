"""WeBridge 渠道后台的本地数据视图。

好友、群组和群成员只读取同步命令已经落库的资料（见 service.persist_directory_result），
消息读取统一消息表。每个标签页都支持关键字搜索和分页，页面只渲染当前标签的数据。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Chatroom, ChatroomMember, Channel, Conversation, Message, WorkWechatContact
from app.services.identity_directory import IdentityDirectory, make_identity

PAGE_SIZE = 20
TABS = ("contacts", "groups", "members", "messages")


def _avatar(extra_json: str | None) -> str:
    """读取群和群成员 extra_json 中的头像地址，解析失败按无头像处理。"""

    try:
        extra = json.loads(extra_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    return str(extra.get("avatar_url") or "").strip() if isinstance(extra, dict) else ""


def _like(keyword: str) -> str:
    """转义 LIKE 通配符，避免用户输入 % 或 _ 时匹配到全部数据。"""

    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _paginate(db: Session, stmt, page: int) -> tuple[list, dict[str, Any]]:
    """对已带过滤条件的查询统计总数并取当前页。"""

    total = int(db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    current = min(max(int(page or 1), 1), total_pages)
    rows = db.execute(stmt.offset((current - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return rows, {
        "page": current,
        "total": total,
        "total_pages": total_pages,
        "has_prev": current > 1,
        "has_next": current < total_pages,
        "prev_page": max(current - 1, 1),
        "next_page": min(current + 1, total_pages),
    }


def build_webridge_dashboard(db: Session, channel: Channel, *, tab: str = "contacts", keyword: str = "", page: int = 1) -> dict:
    """构建单个 WeBridge 渠道的总览：各标签数量加当前标签的搜索分页结果。"""

    active = tab if tab in TABS else "contacts"
    q = (keyword or "").strip()[:100]
    cid = int(channel.id)

    contact_filter = [WorkWechatContact.channel_id == cid, WorkWechatContact.contact_kind == "friend"]
    counts = {
        "contacts": int(db.scalar(select(func.count(WorkWechatContact.id)).where(*contact_filter)) or 0),
        "groups": int(db.scalar(select(func.count(Chatroom.id)).where(Chatroom.channel_id == cid)) or 0),
        "members": int(db.scalar(select(func.count(ChatroomMember.id)).where(ChatroomMember.channel_id == cid)) or 0),
        "messages": int(
            db.scalar(
                select(func.count(Message.id)).join(Conversation, Message.conversation_id == Conversation.id).where(Conversation.channel_id == cid)
            )
            or 0
        ),
    }
    # 外部 ID -> 会话 ID，用于“查看消息”链接；只取本页涉及的 ID，避免整渠道扫描。
    result: dict[str, Any] = {"tab": active, "keyword": q, "counts": counts, "rows": [], "message_identities": {}}

    if active == "contacts":
        stmt = select(WorkWechatContact).where(*contact_filter)
        if q:
            like = _like(q)
            stmt = stmt.where(
                or_(
                    WorkWechatContact.display_name.like(like, escape="\\"),
                    WorkWechatContact.name.like(like, escape="\\"),
                    WorkWechatContact.contact_external_id.like(like, escape="\\"),
                )
            )
        rows, pagination = _paginate(db, stmt.order_by(WorkWechatContact.display_name.asc(), WorkWechatContact.id.desc()), page)
        contacts = [row[0] for row in rows]
        conv_map = _conversation_ids(db, cid, [item.contact_external_id for item in contacts])
        result["rows"] = [
            {
                "identity": make_identity(item.contact_external_id, item.display_name or item.name, item.avatar_url),
                "remark": item.display_name if item.name and item.display_name != item.name else "",
                "last_synced_at": item.last_synced_at,
                "conversation_id": conv_map.get(item.contact_external_id),
            }
            for item in contacts
        ]
    elif active == "groups":
        stmt = select(Chatroom).where(Chatroom.channel_id == cid)
        if q:
            like = _like(q)
            stmt = stmt.where(or_(Chatroom.name.like(like, escape="\\"), Chatroom.remark.like(like, escape="\\"), Chatroom.external_id.like(like, escape="\\")))
        rows, pagination = _paginate(db, stmt.order_by(Chatroom.name.asc(), Chatroom.id.desc()), page)
        rooms = [row[0] for row in rows]
        conv_map = _conversation_ids(db, cid, [item.external_id for item in rooms])
        result["rows"] = [
            {
                "identity": make_identity(item.external_id, item.remark or item.name, _avatar(item.extra_json)),
                "room_id": int(item.id),
                "member_count": item.member_count,
                "last_synced_at": item.last_synced_at,
                "conversation_id": conv_map.get(item.external_id),
            }
            for item in rooms
        ]
    elif active == "members":
        stmt = select(ChatroomMember, Chatroom).join(Chatroom, ChatroomMember.chatroom_id == Chatroom.id).where(ChatroomMember.channel_id == cid)
        if q:
            like = _like(q)
            stmt = stmt.where(
                or_(
                    ChatroomMember.nickname.like(like, escape="\\"),
                    ChatroomMember.display_name.like(like, escape="\\"),
                    ChatroomMember.member_external_id.like(like, escape="\\"),
                    Chatroom.name.like(like, escape="\\"),
                )
            )
        rows, pagination = _paginate(db, stmt.order_by(Chatroom.name.asc(), ChatroomMember.id.asc()), page)
        result["rows"] = [
            {
                "identity": make_identity(member.member_external_id, member.display_name or member.nickname, _avatar(member.extra_json)),
                "role": member.role,
                "last_synced_at": member.last_synced_at,
                "room": make_identity(room.external_id, room.remark or room.name, _avatar(room.extra_json)),
                "room_id": int(room.id),
            }
            for member, room in rows
        ]
    else:
        stmt = select(Message).join(Conversation, Message.conversation_id == Conversation.id).where(Conversation.channel_id == cid)
        if q:
            like = _like(q)
            stmt = stmt.where(
                or_(Message.content.like(like, escape="\\"), Message.sender_name.like(like, escape="\\"), Message.sender_id.like(like, escape="\\"))
            )
        rows, pagination = _paginate(db, stmt.order_by(Message.id.desc()), page)
        messages = [row[0] for row in rows]
        # 查询按最新优先分页，模板内再反转，保证页内最新消息在底部。
        result["rows"] = messages
        result["message_identities"] = IdentityDirectory(db).for_messages(messages)

    result["pagination"] = pagination
    return result


def _conversation_ids(db: Session, channel_id: int, external_ids: list[str]) -> dict[str, int]:
    """批量查询外部 ID 对应的会话，供列表直接跳转到聊天记录。"""

    ids = [item for item in external_ids if item]
    if not ids:
        return {}
    rows = db.execute(select(Conversation.external_id, Conversation.id).where(Conversation.channel_id == channel_id, Conversation.external_id.in_(ids))).all()
    return {external_id: int(conversation_id) for external_id, conversation_id in rows}
