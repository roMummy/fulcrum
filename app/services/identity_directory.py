"""统一的昵称头像解析：把 wxid 等外部 ID 换成后台可读的昵称与头像。

群消息优先使用本群成员资料，其他场景优先好友资料；资料不完整时回退到消息自带 sender_name。
页面只负责渲染“头像 + 昵称 + 下一行 ID”，不再自行拼接 wxid。
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, Message, WorkWechatContact
from app.services.message_actions import is_own_message, object_json

# 形如 wxid_xxx、xxx@chatroom、gh_xxx 的字符串属于内部 ID，不适合作为昵称展示。
_RAW_ID_PATTERN = re.compile(r"^(wxid_[0-9a-z]+|gh_[0-9a-z]+|[0-9a-z_\-]+@chatroom|[0-9]+@im\.wxwork)$", re.IGNORECASE)


def looks_like_raw_id(value: object) -> bool:
    """判断文本是否只是内部 ID，避免把 sender_name 里存的 wxid 当作昵称。"""

    return bool(_RAW_ID_PATTERN.match(str(value or "").strip()))


def _readable_name(external_id: object, *candidates: object) -> str:
    for candidate in candidates:
        name = str(candidate or "").strip()
        if name and name != str(external_id or "").strip() and not looks_like_raw_id(name):
            return name
    return ""


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
        self._cache: dict[tuple[int, str, str], dict] = {}
        self._own_cache: dict[int, dict] = {}

    def preload(self, channel_id: int | None, external_ids: Iterable[object], *, group_external_id: str = "") -> None:
        """批量加载一个渠道下的好友、群成员和群档案，避免逐条查询。"""

        cid = int(channel_id or 0)
        ids = {str(item or "").strip() for item in external_ids}
        ids = {item for item in ids if item and (cid, group_external_id, item) not in self._cache}
        if not cid or not ids:
            return
        found: dict[str, dict] = {}

        def merge(ext: str, name: str, avatar: str) -> None:
            previous = found.get(ext) or {}
            if name or avatar:
                # 名称与头像分别补齐，只有备注的好友记录不能抹掉已同步的成员头像。
                found[ext] = make_identity(ext, name or previous.get("name"), avatar or previous.get("avatar_url"))

        for room in self.db.scalars(select(Chatroom).where(Chatroom.channel_id == cid, Chatroom.external_id.in_(ids))).all():
            merge(room.external_id, _readable_name(room.external_id, room.remark, room.name), _avatar_from_extra(room.extra_json))
        member_query = select(ChatroomMember).where(ChatroomMember.channel_id == cid, ChatroomMember.member_external_id.in_(ids))
        if group_external_id:
            member_query = member_query.join(Chatroom, Chatroom.id == ChatroomMember.chatroom_id).where(
                Chatroom.channel_id == cid, Chatroom.external_id == group_external_id,
            )
        members = self.db.scalars(member_query.order_by(ChatroomMember.id.asc())).all()
        contacts = self.db.scalars(
            select(WorkWechatContact).where(WorkWechatContact.channel_id == cid, WorkWechatContact.contact_external_id.in_(ids))
        ).all()
        member_profiles = [(item.member_external_id, _readable_name(item.member_external_id, item.display_name, item.nickname), _avatar_from_extra(item.extra_json)) for item in members]
        contact_profiles = [(item.contact_external_id, _readable_name(item.contact_external_id, item.display_name, item.name), str(item.avatar_url or "").strip()) for item in contacts]
        # 群消息以本群昵称为准；私聊等无群上下文的场景保留好友备注优先。
        profiles = contact_profiles + member_profiles if group_external_id else member_profiles + contact_profiles
        for ext, name, avatar in profiles:
            merge(ext, name, avatar)
        for ext in ids:
            self._cache[(cid, group_external_id, ext)] = found.get(ext) or {}

    def get(self, channel_id: int | None, external_id: object, fallback_name: object = "", *, group_external_id: str = "") -> dict:
        """读取身份；未同步资料时退回消息自带昵称，再退回 ID。"""

        cid = int(channel_id or 0)
        ext = str(external_id or "").strip()
        if cid and ext and (cid, group_external_id, ext) not in self._cache:
            self.preload(cid, [ext], group_external_id=group_external_id)
        hit = self._cache.get((cid, group_external_id, ext)) or {}
        if hit:
            return make_identity(ext, _readable_name(ext, hit["name"], fallback_name), hit["avatar_url"])
        return make_identity(ext, fallback_name, "")

    def _own_identity(self, channel: Channel | None) -> dict:
        """自己的身份只取渠道账号资料，不随发送占位记录或手机回显变化。"""

        if channel is None:
            return {**make_identity("", "我方"), "show_id": False, "is_own": True}
        cid = int(channel.id)
        if cid not in self._own_cache:
            # account_identity 也引用本模块的 make_identity，延迟导入避免循环依赖。
            from app.services.account_identity import account_identity

            account = account_identity(channel)
            own_id = account["external_id"]
            if not own_id and channel.channel_type == "wechat_ipad":
                own_id = str(object_json(channel.adapter_config_json).get("wxid") or "").strip()
            if not own_id and channel.provider_account_id and channel._sa_instance_state.session is not None:
                bound = channel.provider_account
                own_id = str(bound.external_user_id or "").strip() if bound is not None else ""
            # 只允许用已确认的本账号 ID 补资料，不能从当前会话的对方或临时 sender_name 取头像昵称。
            directory = self.get(cid, own_id) if own_id else {}
            name = account["name"]
            if not name or name == own_id or looks_like_raw_id(name) or is_system_sender(name):
                name = directory.get("name") or ""
            if not name or name == own_id or looks_like_raw_id(name) or is_system_sender(name):
                name = "我方"
            avatar = account["avatar_url"] or directory.get("avatar_url") or ""
            self._own_cache[cid] = {**make_identity(own_id, name, avatar), "show_id": False, "is_own": True}
        return self._own_cache[cid]

    def for_messages(self, messages: Iterable[Message]) -> dict[int, dict]:
        """返回 message.id -> 发送者身份；一次性按渠道批量预加载。"""

        rows = list(messages)
        conv_ids = {int(item.conversation_id) for item in rows}
        conv_context = {
            int(c.id): (int(c.channel_id or 0), str(c.external_id or "") if c.chat_type == "group" else "")
            for c in (self.db.scalars(select(Conversation).where(Conversation.id.in_(conv_ids))).all() if conv_ids else [])
        }
        by_context: dict[tuple[int, str], set[str]] = {}
        channel_ids = {context[0] for context in conv_context.values()} - {0}
        channels = {
            int(channel.id): channel
            for channel in (self.db.scalars(select(Channel).where(Channel.id.in_(channel_ids))).all() if channel_ids else [])
        }
        for item in rows:
            if item.sender_id:
                by_context.setdefault(conv_context.get(int(item.conversation_id), (0, "")), set()).add(item.sender_id)
        for (cid, group_id), ids in by_context.items():
            self.preload(cid, ids, group_external_id=group_id)
        result: dict[int, dict] = {}
        for item in rows:
            cid, group_id = conv_context.get(int(item.conversation_id), (0, ""))
            own = is_own_message(item, channels.get(cid))
            if own:
                result[int(item.id)] = self._own_identity(channels.get(cid))
                continue
            if is_system_sender(item.sender_id) or (item.direction == "out" and not item.sender_id):
                # 出站消息展示为“我方”，不暴露 outbound-adapter 之类的内部标识。
                result[int(item.id)] = {**make_identity("", "我方", ""), "show_id": False, "is_own": own}
                continue
            # 手机回传的本账号消息可能仍存为入站，显示时按渠道账号识别，兼容已有记录。
            result[int(item.id)] = {**self.get(cid, item.sender_id, item.sender_name, group_external_id=group_id), "is_own": own}
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


