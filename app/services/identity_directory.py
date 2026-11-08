"""统一的昵称头像解析：把 wxid 等外部 ID 换成后台可读的昵称与头像。

数据来源按优先级：好友资料（备注/昵称）> 群成员资料 > 群档案 > 会话标题 > 消息自带 sender_name。
页面只负责渲染“头像 + 昵称 + 下一行 ID”，不再自行拼接 wxid。
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chatroom, ChatroomMember, Conversation, Message, WorkWechatContact

# 形如 wxid_xxx、xxx@chatroom、gh_xxx 的字符串属于内部 ID，不适合作为昵称展示。
_RAW_ID_PATTERN = re.compile(r"^(wxid_[0-9a-z]+|gh_[0-9a-z]+|[0-9a-z_\-]+@chatroom|[0-9]+@im\.wxwork)$", re.IGNORECASE)


def looks_like_raw_id(value: object) -> bool:
    """判断文本是否只是内部 ID，避免把 sender_name 里存的 wxid 当作昵称。"""

    return bool(_RAW_ID_PATTERN.match(str(value or "").strip()))


def _avatar_from_extra(extra_json: str | None) -> str:
    """从 extra_json 中取头像地址，字段兼容 persist_directory_result 的写入格式。"""

    try:
        extra = json.loads(extra_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(extra, dict):
        return ""
    for key in ("avatar_url", "avatar", "head_img_url"):
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def make_identity(external_id: object, name: object = "", avatar_url: object = "") -> dict:
    """生成统一的身份结构；名称缺失时显示 ID 本身，但仍保留 external_id 供第二行展示。"""

    ext = str(external_id or "").strip()
    label = str(name or "").strip()
    if not label or looks_like_raw_id(label):
        label = label or ext or "未知"
    return {
        "external_id": ext,
        "name": label,
        "avatar_url": str(avatar_url or "").strip(),
        "initial": (label[:1] or "?").upper(),
        # 昵称与 ID 相同时第二行没有额外信息，模板据此隐藏。
        "show_id": bool(ext) and ext != label,
    }


class IdentityDirectory:
    """按渠道缓存已查询的身份，同一页面内多次解析不重复访问数据库。"""

    def __init__(self, db: Session):
        self.db = db
        self._cache: dict[tuple[int, str], dict] = {}

    def preload(self, channel_id: int | None, external_ids: Iterable[object]) -> None:
        """批量加载一个渠道下的好友、群成员和群档案，避免逐条查询。"""

        cid = int(channel_id or 0)
        ids = {str(item or "").strip() for item in external_ids}
        ids = {item for item in ids if item and (cid, item) not in self._cache}
        if not cid or not ids:
            return
        found: dict[str, dict] = {}
        for room in self.db.scalars(select(Chatroom).where(Chatroom.channel_id == cid, Chatroom.external_id.in_(ids))).all():
            found[room.external_id] = make_identity(room.external_id, room.remark or room.name, _avatar_from_extra(room.extra_json))
        for item in self.db.scalars(
            select(ChatroomMember).where(ChatroomMember.channel_id == cid, ChatroomMember.member_external_id.in_(ids)).order_by(ChatroomMember.id.asc())
        ).all():
            name = item.display_name or item.nickname
            if name and not looks_like_raw_id(name):
                found[item.member_external_id] = make_identity(item.member_external_id, name, _avatar_from_extra(item.extra_json))
        # 好友资料优先级最高，最后写入以覆盖群成员里的群昵称。
        for contact in self.db.scalars(
            select(WorkWechatContact).where(WorkWechatContact.channel_id == cid, WorkWechatContact.contact_external_id.in_(ids))
        ).all():
            name = contact.display_name or contact.name
            if name and not looks_like_raw_id(name):
                found[contact.contact_external_id] = make_identity(contact.contact_external_id, name, contact.avatar_url)
        for ext in ids:
            self._cache[(cid, ext)] = found.get(ext) or {}

    def get(self, channel_id: int | None, external_id: object, fallback_name: object = "") -> dict:
        """读取身份；未同步资料时退回消息自带昵称，再退回 ID。"""

        cid = int(channel_id or 0)
        ext = str(external_id or "").strip()
        if cid and ext and (cid, ext) not in self._cache:
            self.preload(cid, [ext])
        hit = self._cache.get((cid, ext)) or {}
        if hit:
            return hit
        return make_identity(ext, fallback_name, "")

    def for_messages(self, messages: Iterable[Message]) -> dict[int, dict]:
        """返回 message.id -> 发送者身份；一次性按渠道批量预加载。"""

        rows = list(messages)
        conv_ids = {int(item.conversation_id) for item in rows}
        conv_channel = {
            int(c.id): int(c.channel_id or 0)
            for c in (self.db.scalars(select(Conversation).where(Conversation.id.in_(conv_ids))).all() if conv_ids else [])
        }
        by_channel: dict[int, set[str]] = {}
        for item in rows:
            if item.sender_id:
                by_channel.setdefault(conv_channel.get(int(item.conversation_id), 0), set()).add(item.sender_id)
        for cid, ids in by_channel.items():
            self.preload(cid, ids)
        result: dict[int, dict] = {}
        for item in rows:
            cid = conv_channel.get(int(item.conversation_id), 0)
            if is_system_sender(item.sender_id) or (item.direction == "out" and not item.sender_id):
                # 出站消息展示为“我方”，不暴露 outbound-adapter 之类的内部标识。
                result[int(item.id)] = {**make_identity("", "我方", ""), "show_id": False}
                continue
            result[int(item.id)] = self.get(cid, item.sender_id, item.sender_name)
        return result

    def for_conversations(self, conversations: Iterable[Conversation]) -> dict[int, dict]:
        """返回 conversation.id -> 会话对象身份（好友或群），标题缺失时回退到资料表。"""

        rows = list(conversations)
        by_channel: dict[int, set[str]] = {}
        for item in rows:
            by_channel.setdefault(int(item.channel_id or 0), set()).add(item.external_id)
        for cid, ids in by_channel.items():
            self.preload(cid, ids)
        result: dict[int, dict] = {}
        for item in rows:
            ident = self.get(item.channel_id, item.external_id, item.title)
            # 会话标题由事件写入，通常是最新昵称；资料表没有头像时保留标题作为名称。
            if item.title and not looks_like_raw_id(item.title) and ident["name"] != item.title and not ident.get("avatar_url"):
                ident = make_identity(item.external_id, item.title, ident.get("avatar_url"))
            result[int(item.id)] = ident
        return result
# 后台和适配器写入出站消息时使用的内部发送者前缀，它们不是微信 ID，只显示为“我方”。
_SYSTEM_SENDER_PREFIXES = ("outbound-adapter:", "admin-console", "system")


def is_system_sender(sender_id: object) -> bool:
    return str(sender_id or "").strip().startswith(_SYSTEM_SENDER_PREFIXES)


