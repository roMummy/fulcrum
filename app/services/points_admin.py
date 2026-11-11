"""积分管理后台的查询与人工调整。

页面两块:
- 账户:所有群的积分账户,带头像昵称,可按群、关键字筛选,并对单个账户人工发放/扣减。
- 账单:积分流水,可按群、用户、来源筛选,每条显示变动值、变动后余额和来源。
所有读写都经过 PointsService,与插件赚取、消费使用同一份账户和流水,口径一致。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.entities import Chatroom, ChatroomMember, PointsAccount, PointsTransaction, WorkWechatContact
from app.services.directory_pages import PAGE_SIZE, _like, _page
from app.services.identity_directory import _avatar_from_extra, looks_like_raw_id, make_identity
from app.services.points import PointsResult, PointsService

# 人工调整的来源标记;账单筛选里归入“人工调整”。
ADMIN_SOURCE = "admin_adjust"
# 单次人工调整的上限,防止手误输入天文数字。
MAX_ADJUST_POINTS = 1_000_000

# 来源代码到中文名;未收录的来源原样显示,新插件无需改这里也能出账单。
SOURCE_LABELS = {
    "checkin": "签到奖励",
    "checkin_makeup": "补签消耗",
    "trade": "积分转账",
    "game_task": "游戏任务",
    "lucky_pot_entry": "大奖池入场",
    "lucky_pot_refund": "大奖池退款",
    "daily_ranking": "活跃榜奖励",
    "cultivation_exchange": "修仙兑换",
    ADMIN_SOURCE: "人工调整",
}


def _room_names(db: Session, group_ids: set[str]) -> dict[str, dict[str, Any]]:
    """群 external_id -> 群身份(昵称头像),按群档案批量解析。"""

    if not group_ids:
        return {}
    rooms = db.scalars(select(Chatroom).where(Chatroom.external_id.in_(group_ids))).all()
    return {room.external_id: make_identity(room.external_id, room.remark or room.name, _avatar_from_extra(room.extra_json)) for room in rooms}


def _user_identities(db: Session, pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(群, 用户) -> 用户身份。优先好友资料,其次本群成员资料,最后账户里记的昵称。"""

    user_ids = {user for _group, user in pairs}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    if not user_ids:
        return result
    contacts: dict[str, WorkWechatContact] = {}
    for item in db.scalars(select(WorkWechatContact).where(WorkWechatContact.contact_external_id.in_(user_ids))).all():
        if (item.display_name or item.name) and not looks_like_raw_id(item.display_name or item.name):
            contacts[item.contact_external_id] = item
    members: dict[tuple[str, str], ChatroomMember] = {}
    rows = db.execute(
        select(Chatroom.external_id, ChatroomMember)
        .join(Chatroom, Chatroom.id == ChatroomMember.chatroom_id)
        .where(ChatroomMember.member_external_id.in_(user_ids))
    ).all()
    for group_id, member in rows:
        name = member.display_name or member.nickname
        if name and not looks_like_raw_id(name):
            members[(str(group_id), member.member_external_id)] = member
    for group_id, user_id in pairs:
        member = members.get((group_id, user_id))
        contact = contacts.get(user_id)
        if contact is not None:
            result[(group_id, user_id)] = make_identity(user_id, contact.display_name or contact.name, contact.avatar_url)
        elif member is not None:
            result[(group_id, user_id)] = make_identity(user_id, member.display_name or member.nickname, _avatar_from_extra(member.extra_json))
    return result


def _identity_for(resolved: dict[tuple[str, str], dict[str, Any]], group_id: str, user_id: str, fallback_name: str | None) -> dict[str, Any]:
    return resolved.get((group_id, user_id)) or make_identity(user_id, fallback_name or "")


def group_options(db: Session) -> list[dict[str, Any]]:
    """可选的群:所有已纳管的群,加上有过积分账户的群。

    没人有积分的新群也必须能选到,否则无法给里面的成员发放第一笔积分。
    """

    rooms = db.scalars(select(Chatroom).where(Chatroom.management_status == "enabled")).all()
    names = {room.external_id: (room.remark or room.name or room.external_id) for room in rooms}
    extra_ids = {str(item) for item in db.scalars(select(PointsAccount.group_id).distinct()).all() if item} - set(names)
    for gid, identity in _room_names(db, extra_ids).items():
        names[gid] = identity["name"]
    for gid in extra_ids:
        names.setdefault(gid, gid)
    return sorted(({"id": gid, "name": name} for gid, name in names.items()), key=lambda item: str(item["name"]))


def build_points_accounts(db: Session, *, keyword: str = "", group_id: str = "", page: int = 1) -> dict[str, Any]:
    """账户列表,运营在这里先选到某个人,再对他调整积分。

    - 未选群:列出所有已有积分账户的 群+用户,按积分从高到低。
    - 选了群:列出该群全部成员,没有积分的成员显示 0,同样可以被选中发放。
    """

    q = (keyword or "").strip()[:100]
    if group_id:
        return _group_member_accounts(db, group_id=group_id, keyword=q, page=page)
    stmt = select(PointsAccount)
    if q:
        like = _like(q)
        stmt = stmt.where(or_(PointsAccount.user_name.like(like, escape="\\"), PointsAccount.user_id.like(like, escape="\\")))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    paging = _page(total, page)
    accounts = db.scalars(stmt.order_by(PointsAccount.total_points.desc(), PointsAccount.id.asc()).offset(paging["offset"]).limit(PAGE_SIZE)).all()
    rooms = _room_names(db, {item.group_id for item in accounts})
    resolved = _user_identities(db, {(item.group_id, item.user_id) for item in accounts})
    rows = [
        {
            "identity": _identity_for(resolved, item.group_id, item.user_id, item.user_name),
            "group": rooms.get(item.group_id) or make_identity(item.group_id, ""),
            "group_id": item.group_id,
            "user_id": item.user_id,
            "points": int(item.total_points or 0),
        }
        for item in accounts
    ]
    totals = db.execute(select(func.count(PointsAccount.id), func.coalesce(func.sum(PointsAccount.total_points), 0))).one()
    return {
        "rows": rows,
        "paging": paging,
        "keyword": q,
        "group_id": "",
        "summary": {"accounts": int(totals[0] or 0), "points": int(totals[1] or 0)},
    }


def _group_member_accounts(db: Session, *, group_id: str, keyword: str, page: int) -> dict[str, Any]:
    """选中某个群后的成员清单:群成员并上已有账户,没有积分的成员记 0。"""

    room = db.scalar(select(Chatroom).where(Chatroom.external_id == group_id))
    balances = PointsService(db).group_balances(group_id)
    names = {str(u): n for u, n in db.execute(select(PointsAccount.user_id, PointsAccount.user_name).where(PointsAccount.group_id == group_id)).all()}
    member_ids: set[str] = set(balances)
    if room is not None:
        member_ids |= {
            str(x)
            for x in db.scalars(
                select(ChatroomMember.member_external_id).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.status != "left")
            ).all()
        }
    resolved = _user_identities(db, {(group_id, uid) for uid in member_ids})
    group_identity = _room_names(db, {group_id}).get(group_id) or make_identity(group_id, "")
    rows = []
    for uid in member_ids:
        identity = _identity_for(resolved, group_id, uid, names.get(uid))
        if keyword and keyword.lower() not in f"{identity['name']} {uid}".lower():
            continue
        rows.append({"identity": identity, "group": group_identity, "group_id": group_id, "user_id": uid, "points": balances.get(uid, 0)})
    # 有积分的在前,同分按昵称排,翻页稳定。
    rows.sort(key=lambda item: (-item["points"], item["identity"]["name"]))
    paging = _page(len(rows), page)
    return {
        "rows": rows[paging["offset"] : paging["offset"] + PAGE_SIZE],
        "paging": paging,
        "keyword": keyword,
        "group_id": group_id,
        "summary": {"accounts": len(balances), "points": sum(balances.values())},
    }


def build_points_ledger(db: Session, *, keyword: str = "", group_id: str = "", source: str = "", page: int = 1) -> dict[str, Any]:
    """账单:积分流水,最新在前。"""

    q = (keyword or "").strip()[:100]
    stmt = select(PointsTransaction)
    if group_id:
        stmt = stmt.where(PointsTransaction.group_id == group_id)
    if source:
        stmt = stmt.where(PointsTransaction.source == source)
    if q:
        like = _like(q)
        name_match = select(PointsAccount.user_id).where(PointsAccount.user_name.like(like, escape="\\"))
        stmt = stmt.where(or_(PointsTransaction.user_id.like(like, escape="\\"), PointsTransaction.user_id.in_(name_match)))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    paging = _page(total, page)
    items = db.scalars(stmt.order_by(PointsTransaction.id.desc()).offset(paging["offset"]).limit(PAGE_SIZE)).all()
    rooms = _room_names(db, {item.group_id for item in items})
    resolved = _user_identities(db, {(item.group_id, item.user_id) for item in items})
    names = {
        (str(g), str(u)): n
        for g, u, n in db.execute(
            select(PointsAccount.group_id, PointsAccount.user_id, PointsAccount.user_name).where(
                PointsAccount.user_id.in_({item.user_id for item in items} or {""})
            )
        ).all()
    }
    rows = []
    for item in items:
        try:
            meta = json.loads(item.metadata_json or "{}")
        except (TypeError, ValueError):
            meta = {}
        rows.append(
            {
                "id": int(item.id),
                "created_at": item.created_at,
                "identity": _identity_for(resolved, item.group_id, item.user_id, names.get((item.group_id, item.user_id))),
                "group": rooms.get(item.group_id) or make_identity(item.group_id, ""),
                "delta": int(item.points_delta or 0),
                "balance_after": int(item.balance_after or 0),
                "source_label": SOURCE_LABELS.get(item.source, item.source),
                "description": item.description or "",
                "operator": str(meta.get("operator") or "") if isinstance(meta, dict) else "",
            }
        )
    used_sources = [str(s) for s in db.scalars(select(PointsTransaction.source).distinct()).all() if s]
    source_options = [{"id": s, "name": SOURCE_LABELS.get(s, s)} for s in sorted(used_sources)]
    return {"rows": rows, "paging": paging, "keyword": q, "group_id": group_id, "source": source, "source_options": source_options}


def adjust_points(db: Session, *, group_id: str, user_id: str, amount: int, operator: str, note: str = "", request_id: str = "") -> PointsResult:
    """人工调整:amount>0 发放,amount<0 扣减。每次调整生成一条带操作人的流水。"""

    group_id = str(group_id or "").strip()
    user_id = str(user_id or "").strip()
    if not group_id or not user_id:
        return PointsResult(ok=False, message="请选择群和成员。")
    if amount == 0 or abs(amount) > MAX_ADJUST_POINTS:
        return PointsResult(ok=False, message=f"积分必须在 1 到 {MAX_ADJUST_POINTS} 之间。")
    service = PointsService(db)
    existing = db.scalar(select(PointsAccount).where(PointsAccount.group_id == group_id, PointsAccount.user_id == user_id))
    if existing is None:
        # 只允许对已有账户或本群已知成员发放,避免手误输入凭空建账户。
        room = db.scalar(select(Chatroom).where(Chatroom.external_id == group_id))
        known = room is not None and db.scalar(
            select(ChatroomMember.id).where(ChatroomMember.chatroom_id == room.id, ChatroomMember.member_external_id == user_id)
        )
        if not known:
            return PointsResult(ok=False, message="该成员不在本群成员列表中,请先同步群成员。")
    description = (note or "").strip()[:200] or ("后台发放" if amount > 0 else "后台扣减")
    # 幂等键来自表单渲染时生成的一次性令牌:同一份表单被重复提交(双击、刷新重发)只产生一条流水。
    key = f"admin:{request_id or uuid.uuid4().hex}"
    member_name = _member_name(db, group_id, user_id)
    kwargs = {"user_name": member_name, "source": ADMIN_SOURCE, "description": description, "metadata": {"operator": operator}, "idempotency_key": key}
    if amount > 0:
        return service.add_points(user_id, group_id, amount, **kwargs)
    return service.deduct_points(user_id, group_id, -amount, **kwargs)


def _member_name(db: Session, group_id: str, user_id: str) -> str | None:
    """取成员在本群的真实昵称,用于新建账户时记录;只有 wxid 时返回 None。"""

    member = db.scalar(
        select(ChatroomMember)
        .join(Chatroom, Chatroom.id == ChatroomMember.chatroom_id)
        .where(Chatroom.external_id == group_id, ChatroomMember.member_external_id == user_id)
    )
    name = (member.display_name or member.nickname) if member is not None else ""
    return name if name and not looks_like_raw_id(name) else None
