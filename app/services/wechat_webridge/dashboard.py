"""WeBridge 渠道后台的本地数据视图。

好友、群组和群成员只读取同步命令已经落库的资料（见 service.persist_directory_result），
消息读取统一消息表。每个标签页都支持关键字搜索和分页，页面只渲染当前标签的数据。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Chatroom, ChatroomMember, Channel, Conversation, Message, WebridgeDeviceSample, WorkWechatContact
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


def build_webridge_dashboard(
    db: Session,
    channel: Channel,
    *,
    tab: str = "contacts",
    keyword: str = "",
    page: int = 1,
    conversation_id: int = 0,
    sender: str = "",
) -> dict:
    """构建单个 WeBridge 渠道的总览：各标签数量加当前标签的搜索分页结果。"""

    active = tab if tab in TABS else "contacts"
    q = (keyword or "").strip()[:100]
    sender_q = (sender or "").strip()[:100]
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
    result: dict[str, Any] = {"tab": active, "keyword": q, "sender": sender_q, "conversation_id": int(conversation_id or 0), "conversations": [], "counts": counts, "rows": [], "message_identities": {}}

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
        # 会话筛选（群或好友）：限定在本渠道内，避免跨渠道读取。
        if conversation_id:
            stmt = stmt.where(Message.conversation_id == int(conversation_id))
        # 发送者筛选：按昵称或 wxid 模糊匹配。
        if sender_q:
            like = _like(sender_q)
            stmt = stmt.where(or_(Message.sender_name.like(like, escape="\\"), Message.sender_id.like(like, escape="\\")))
        rows, pagination = _paginate(db, stmt.order_by(Message.id.desc()), page)
        messages = [row[0] for row in rows]
        # 查询按最新优先分页，模板内再反转，保证页内最新消息在底部。
        result["rows"] = messages
        directory = IdentityDirectory(db)
        result["message_identities"] = directory.for_messages(messages)
        # 筛选下拉：按最近活跃排序的会话，最多 200 个，展示昵称而不是 wxid。
        convs = db.scalars(
            select(Conversation).where(Conversation.channel_id == cid).order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(200)
        ).all()
        conv_identities = directory.for_conversations(convs)
        result["conversations"] = [
            {"id": int(item.id), "name": (conv_identities.get(item.id) or {}).get("name") or item.title or item.external_id, "is_group": item.chat_type == "group"}
            for item in convs
        ]

    result["pagination"] = pagination
    return result


def _conversation_ids(db: Session, channel_id: int, external_ids: list[str]) -> dict[str, int]:
    """批量查询外部 ID 对应的会话，供列表直接跳转到聊天记录。"""

    ids = [item for item in external_ids if item]
    if not ids:
        return {}
    rows = db.execute(select(Conversation.external_id, Conversation.id).where(Conversation.channel_id == channel_id, Conversation.external_id.in_(ids))).all()
    return {external_id: int(conversation_id) for external_id, conversation_id in rows}


# 手机 state 上报字段 -> 页面标签。只展示微桥已经给出的字段，未识别字段统一进“其他上报”。
STATE_LABELS = (
    ("is_logged_in", "微信登录", "bool"),
    ("auth_status", "授权状态", "text"),
    ("auth_expires_at", "授权有效期", "text"),
    ("send_enabled", "发送开关", "bool"),
    ("receive_enabled", "接收开关", "bool"),
    ("forward_enabled", "转发开关", "bool"),
    ("plugin_enabled", "插件启用", "bool"),
    ("event_uploader_running", "事件上传线程", "bool"),
    ("log_capture_enabled", "日志采集", "bool"),
    ("cached_message_count", "缓存消息数", "text"),
    ("cached_room_snapshot_count", "缓存群快照", "text"),
    ("cached_red_packet_count", "缓存红包数", "text"),
    ("pending_event_queue_count", "待上传事件", "text"),
    ("oldest_pending_event_age_ms", "最久待传事件(毫秒)", "text"),
    ("last_hook_selector", "最近 Hook", "text"),
)


def _chart(samples: list[WebridgeDeviceSample], field: str, label_format: str = "%H:%M") -> dict[str, Any]:
    """把采样序列转换为 _dashboard_chart.html 的 trend_chart 宏需要的结构；NULL 采样跳过。"""

    items = [{"label": item.bucket_start.strftime(label_format), "value": int(getattr(item, field))} for item in samples if getattr(item, field) is not None]
    values = [item["value"] for item in items]
    peak = max(values) if values else 0
    points = []
    for index, value in enumerate(values):
        x = 0 if len(values) <= 1 else round(index / (len(values) - 1) * 100, 2)
        y = 100 if peak <= 0 else round(100 - value / peak * 100, 2)
        points.append(f"{x},{y}")
    return {"max_value": peak, "points": " ".join(points), "items": items}


def build_webridge_device_panel(db: Session, channel: Channel, *, hours: int = 24) -> dict[str, Any]:
    """构建 iPhone 状态区：当前在线与上报字段 + 近 hours 小时的积压/缓存趋势。"""

    try:
        runtime = json.loads(channel.runtime_info_json or "{}").get("webridge")
    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
        runtime = None
    runtime = runtime if isinstance(runtime, dict) else {}
    state = runtime.get("state") if isinstance(runtime.get("state"), dict) else {}

    def _show(value: Any, kind: str) -> str:
        if kind == "bool":
            return "是" if value else "否"
        return str(value)

    known = {key for key, _, _ in STATE_LABELS}
    fields = [{"label": label, "value": _show(state[key], kind)} for key, label, kind in STATE_LABELS if key in state]
    extra = [{"label": key, "value": str(value)} for key, value in sorted(state.items()) if key not in known and not isinstance(value, (dict, list))]
    since = now_shanghai() - timedelta(hours=hours)
    samples = db.scalars(
        select(WebridgeDeviceSample).where(WebridgeDeviceSample.channel_id == int(channel.id), WebridgeDeviceSample.bucket_start >= since).order_by(WebridgeDeviceSample.bucket_start.asc())
    ).all()
    # 采样按分钟，24 小时最多 1440 点；每 10 分钟取一个点即可，曲线更平滑且页面更轻。
    samples = samples[::10] if len(samples) > 144 else samples
    capabilities = runtime.get("capabilities") if isinstance(runtime.get("capabilities"), list) else []
    return {
        "online": bool(runtime.get("online")),
        "last_seen_at": runtime.get("last_seen_at"),
        "registered_at": runtime.get("registered_at"),
        "basic": [
            {"label": "运行版本", "value": runtime.get("runtime_version") or "-"},
            {"label": "构建标记", "value": runtime.get("build_marker") or "-"},
            {"label": "微信账号", "value": runtime.get("account_id") or "-"},
            {"label": "设备 ID", "value": runtime.get("device_id") or "-"},
            {"label": "会话 ID", "value": runtime.get("session_id") or "-"},
        ],
        "fields": fields,
        "extra": extra,
        "capability_count": len(capabilities),
        "capabilities": [str(item) for item in capabilities],
        "pending_chart": _chart(samples, "pending_commands"),
        "cache_chart": _chart(samples, "cached_message_count"),
        "event_chart": _chart(samples, "pending_event_queue_count"),
        "sample_count": len(samples),
    }
