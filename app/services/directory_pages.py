"""跨渠道的“群管理”和“好友”列表查询。

两个页面是运营后台里群、好友这两类核心资产的唯一入口:
- 群:所有渠道的群合在一起,支持搜索、渠道与纳管状态筛选,显示头像昵称,外部 ID 只作弱信息。
- 好友:所有渠道的好友合在一起,支持搜索与渠道筛选,并列出共同所在的群。
只读本地已同步的数据,不触发任何渠道同步。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.services.points import PointsService
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, WorkWechatContact
from app.services.chatroom_registry import MANAGEMENT_STATUS_LABELS
from app.services.identity_directory import _avatar_from_extra, make_identity

PAGE_SIZE = 30
# 群运行模式在列表里的中文说法,与群详情共用 chatroom_registry 里的定义。
MANAGEMENT_LABELS = MANAGEMENT_STATUS_LABELS
# 渠道里“好友”类联系人;企微等其他渠道的联系人类型不同,这里只取个人好友。
FRIEND_KIND = "friend"


def _like(keyword: str) -> str:
    """转义 LIKE 通配符,避免输入 % 或 _ 时匹配到全部数据。"""

    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _page(total: int, page: int) -> dict[str, Any]:
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    current = min(max(int(page or 1), 1), pages)
    return {
        "page": current,
        "total": total,
        "total_pages": pages,
        "has_prev": current > 1,
        "has_next": current < pages,
        "offset": (current - 1) * PAGE_SIZE,
    }


def channel_options(db: Session) -> list[dict[str, Any]]:
    """可供筛选的渠道:只列出有群或好友资料的渠道,名称取渠道自己的显示名。"""

    return [{"id": int(item.id), "name": item.name or f"账号 #{item.id}"} for item in db.scalars(select(Channel).where(Channel.deleted_at.is_(None)).order_by(Channel.id.asc())).all()]


def build_chatroom_directory(db: Session, *, keyword: str = "", channel_id: int = 0, status: str = "", page: int = 1) -> dict[str, Any]:
    """群列表:每个群一行,带头像昵称、所属账号、成员数、纳管状态和是否开着插件。"""

    q = (keyword or "").strip()[:100]
    # 已删除账号的群不再出现在列表和汇总里(数据仍保留)。
    visible = Chatroom.channel_id.in_(select(Channel.id).where(Channel.deleted_at.is_(None)))
    stmt = select(Chatroom).where(visible)
    if channel_id:
        stmt = stmt.where(Chatroom.channel_id == channel_id)
    if status == "left":
        # bot 已退出(退群或被移出)的群:只看这一类。
        stmt = stmt.where(Chatroom.status == "left")
    elif status in MANAGEMENT_LABELS:
        stmt = stmt.where(Chatroom.management_status == status, Chatroom.status != "left")
    else:
        status = ""
    if q:
        like = _like(q)
        stmt = stmt.where(or_(Chatroom.name.like(like, escape="\\"), Chatroom.remark.like(like, escape="\\"), Chatroom.external_id.like(like, escape="\\")))

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    paging = _page(total, page)
    rooms = db.scalars(stmt.order_by((Chatroom.status == "left").asc(), Chatroom.is_active.desc(), Chatroom.name.asc(), Chatroom.id.desc()).offset(paging["offset"]).limit(PAGE_SIZE)).all()

    channels = {int(item.id): item for item in db.scalars(select(Channel).where(Channel.deleted_at.is_(None))).all()}
    rows = [
        {
            "id": int(room.id),
            "channel_id": int(room.channel_id),
            "channel_name": (channels[int(room.channel_id)].name if int(room.channel_id) in channels else "") or f"账号 #{room.channel_id}",
            "identity": make_identity(room.external_id, room.remark or room.name, _avatar_from_extra(room.extra_json)),
            "member_count": int(room.member_count or 0),
            "management_status": room.management_status or "discovered",
            "management_label": MANAGEMENT_LABELS.get(room.management_status or "discovered", "待启用"),
            "is_active": bool(room.is_active),
            # bot 已不在这个群里:列表里显示"已退出",不再提供启用开关。
            "is_left": room.status == "left",
            "last_seen_at": room.last_seen_at or room.last_synced_at,
        }
        for room in rooms
    ]
    summary = {
        "total": int(db.scalar(select(func.count(Chatroom.id)).where(visible)) or 0),
        "enabled": int(db.scalar(select(func.count(Chatroom.id)).where(visible, Chatroom.management_status == "enabled", Chatroom.status != "left")) or 0),
        "pending": int(db.scalar(select(func.count(Chatroom.id)).where(visible, Chatroom.management_status == "discovered", Chatroom.status != "left")) or 0),
        "left": int(db.scalar(select(func.count(Chatroom.id)).where(visible, Chatroom.status == "left")) or 0),
    }
    return {"rows": rows, "paging": paging, "keyword": q, "channel_id": channel_id, "status": status, "summary": summary}


def build_contact_directory(db: Session, *, keyword: str = "", channel_id: int = 0, page: int = 1) -> dict[str, Any]:
    """好友列表:每个好友一行,带头像昵称、备注、所属账号、共同所在的群数,并可发起聊天。"""

    q = (keyword or "").strip()[:100]
    stmt = select(WorkWechatContact).where(
        WorkWechatContact.contact_kind == FRIEND_KIND,
        WorkWechatContact.channel_id.in_(select(Channel.id).where(Channel.deleted_at.is_(None))),
    )
    if channel_id:
        stmt = stmt.where(WorkWechatContact.channel_id == channel_id)
    if q:
        like = _like(q)
        stmt = stmt.where(
            or_(
                WorkWechatContact.display_name.like(like, escape="\\"),
                WorkWechatContact.name.like(like, escape="\\"),
                WorkWechatContact.contact_external_id.like(like, escape="\\"),
            )
        )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    paging = _page(total, page)
    contacts = db.scalars(stmt.order_by(WorkWechatContact.display_name.asc(), WorkWechatContact.id.desc()).offset(paging["offset"]).limit(PAGE_SIZE)).all()

    channels = {int(item.id): item for item in db.scalars(select(Channel).where(Channel.deleted_at.is_(None))).all()}
    # 本页好友各自所在的群数、以及与好友的私聊会话,一次性批量取,避免逐行查询。
    external_ids = {item.contact_external_id for item in contacts}
    group_counts: dict[tuple[int, str], int] = {}
    conversation_ids: dict[tuple[int, str], int] = {}
    if external_ids:
        for cid, member_id, count in db.execute(
            select(ChatroomMember.channel_id, ChatroomMember.member_external_id, func.count(ChatroomMember.id))
            .where(ChatroomMember.member_external_id.in_(external_ids), ChatroomMember.status != "left")
            .group_by(ChatroomMember.channel_id, ChatroomMember.member_external_id)
        ).all():
            group_counts[(int(cid), str(member_id))] = int(count or 0)
        for conv in db.scalars(select(Conversation).where(Conversation.external_id.in_(external_ids), Conversation.chat_type == "direct")).all():
            conversation_ids[(int(conv.channel_id or 0), str(conv.external_id))] = int(conv.id)

    totals = PointsService(db).user_totals(sorted(external_ids))
    rows = []
    for item in contacts:
        key = (int(item.channel_id), item.contact_external_id)
        rows.append(
            {
                "channel_id": int(item.channel_id),
                "channel_name": (channels[int(item.channel_id)].name if int(item.channel_id) in channels else "") or f"账号 #{item.channel_id}",
                "identity": make_identity(item.contact_external_id, item.display_name or item.name, item.avatar_url),
                "remark": item.display_name if item.name and item.display_name and item.display_name != item.name else "",
                "group_count": group_counts.get(key, 0),
                "points": totals.get(item.contact_external_id, 0),
                "conversation_id": conversation_ids.get(key),
            }
        )
    return {"rows": rows, "paging": paging, "keyword": q, "channel_id": channel_id}
