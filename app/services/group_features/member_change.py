"""群成员变更提醒:同步群成员加入、退出和群昵称变化,并按群开关在群里发送通知与欢迎卡片。

原先是 group_member_change 插件,现在属于群自身能力:不需要机器人绑定,
在群详情页「功能」里打开即可;成员状态同步和通知都只看本群配置。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember
from app.services.group_features.dispatch import FeatureContext, register_handler
from app.services.config_fields import ConfigField
from app.services.group_features.registry import GroupFeature, register
from app.services.plugin_runtime.replies import PluginReplyError, link_reply, text_reply

logger = get_logger(__name__)

# 每次事件最多给几位新成员发欢迎卡片,防止一次拉很多人时刷屏。
MAX_WELCOME_CARDS = 3

JOIN_PATTERNS = (
    '"$names$"加入了群聊',
    '"$username$"邀请"$names$"加入了群聊',
    '你邀请"$names$"加入了群聊',
    '"$adder$"通过扫描"$from$"分享的二维码加入群聊',
    '"$adder$"通过"$from$"的邀请二维码加入群聊',
    "joined the group",
    "joined the group chat",
)
LEAVE_PATTERNS = (
    '"$username$"退出了群聊',
    '"$username$"离开了群聊',
    '"$names$"退出了群聊',
    '"$names$"离开了群聊',
    "left the group",
    "left the group chat",
)
REMOVED_PATTERNS = (
    '"$username$"将"$names$"移出了群聊',
    '你将"$names$"移出了群聊',
    '"$username$"移出了"$names$"',
    '"$username$"踢出了"$names$"',
    "removed from the group",
    "removed from the group chat",
)
VALID_CHANGE_TYPES = {"join", "left", "removed", "rename"}

FEATURE = register(
    GroupFeature(
        key="member_change",
        name="群成员变更提醒",
        description="成员加入、退出、改群昵称时在群里通知,并可对新成员发送欢迎卡片。",
        event_types=("event", "system", "text", "group_member_join", "group_member_leave", "group_member_nickname_change"),
        fields=(
            ConfigField("notify_join", "新成员加入时通知", kind="bool", default=True, group="通知方式"),
            ConfigField("notify_leave", "成员退出或被移出时通知", kind="bool", default=True, group="通知方式"),
            ConfigField("notify_rename", "成员修改群昵称时通知", kind="bool", default=True, group="通知方式"),
            ConfigField("card_enabled", "新成员加入时发送欢迎卡片", kind="bool", default=False, group="欢迎卡片"),
            ConfigField(
                "card_title", "卡片标题", default="欢迎 {nickname} 加入本群", group="欢迎卡片", depends_on="card_enabled",
                hint="可用变量:{nickname} 新成员昵称,{now} 当前时间",
            ),
            ConfigField("card_desc", "卡片描述", default="{now}", group="欢迎卡片", depends_on="card_enabled", hint="可用变量同上"),
            ConfigField(
                "card_url", "点击后打开的页面", group="欢迎卡片", depends_on="card_enabled",
                hint="必须以 http:// 或 https:// 开头;群成员手机能打开才能点开",
            ),
            ConfigField(
                "card_thumb_url", "缩略图地址", group="欢迎卡片", depends_on="card_enabled",
                hint="可留空;以 http:// 或 https:// 开头的图片地址",
            ),
        ),
    )
)


def handle(ctx: FeatureContext, args: str) -> list[dict[str, Any]] | None:
    """同步成员状态并生成通知;不是成员增减事件的消息返回 None(与本能力无关)。"""

    parsed = parse_member_change(ctx.normalized_event)
    if parsed is None:
        return None

    change_type = str(parsed["change_type"])
    members = list(parsed["members"])
    new_name = str(parsed.get("new_name") or "")
    previous = lookup_previous_names(ctx.db, chatroom=ctx.chatroom, members=members) if change_type == "rename" else {}
    apply_member_change(
        ctx.db, channel=ctx.channel, chatroom=ctx.chatroom,
        change_type=change_type, members=members, new_name=new_name,
    )
    # 微桥事件自带变更后的群人数,直接校准,不必等下一次全量同步。
    if isinstance(parsed.get("after_count"), int):
        ctx.chatroom.member_count = int(parsed["after_count"])

    # 状态同步无论通知开关如何都会做;下面只决定要不要发消息。
    notify_key = {"join": "notify_join", "left": "notify_leave", "removed": "notify_leave", "rename": "notify_rename"}[change_type]
    if not ctx.config.get(notify_key):
        return []

    replies: list[dict[str, Any]] = []
    reply_text = build_reply_text(
        change_type, members,
        new_name=new_name, old_name=str(parsed.get("old_name") or ""), previous=previous,
    )
    if reply_text:
        replies.append(text_reply(reply_text))
    if change_type == "join" and ctx.config.get("card_enabled"):
        replies.extend(build_welcome_cards(ctx.config, members))
    return replies


def build_welcome_cards(config: Mapping[str, Any], members: list[dict[str, str]]) -> list[dict[str, Any]]:
    """为新成员生成欢迎卡片;地址不合法时只记日志并跳过,不影响文字通知。"""

    now_text = now_shanghai().strftime("%Y-%m-%d %H:%M")
    cards: list[dict[str, Any]] = []
    for item in members[:MAX_WELCOME_CARDS]:
        nickname = item.get("display_name") or item.get("external_id") or ""

        def fill(template: str) -> str:
            # 用 replace 而不是 format:配置里可能出现不相干的花括号。
            return str(template or "").replace("{nickname}", nickname).replace("{now}", now_text)

        try:
            cards.append(
                link_reply(
                    url=str(config.get("card_url") or ""),
                    title=fill(config.get("card_title")),
                    description=fill(config.get("card_desc")),
                    thumb_url=str(config.get("card_thumb_url") or ""),
                )
            )
        except PluginReplyError as exc:
            logger.warning("欢迎卡片配置无效,已跳过", extra={"reason": str(exc)})
            break
    return cards


def parse_member_change(normalized_event: Mapping[str, Any]) -> dict[str, Any] | None:
    """先读渠道适配器生成的通用成员事件,没有再按微信系统消息 XML 解析。"""

    parsed = _parse_structured_member_change(normalized_event)
    if parsed is not None:
        return parsed
    message = normalized_event.get("message") if isinstance(normalized_event, Mapping) else None
    content = str((message or {}).get("content") or "").strip() if isinstance(message, Mapping) else ""
    return _parse_wechat_member_change(content)


def _parse_structured_member_change(normalized_event: Mapping[str, Any]) -> dict[str, Any] | None:
    """读取渠道适配器生成的通用成员事件,不直接解析 Telegram 私有字段。"""

    details = normalized_event.get("details") if isinstance(normalized_event, Mapping) else None
    if not isinstance(details, Mapping):
        return None
    member_change = details.get("member_change")
    if not isinstance(member_change, Mapping):
        return None
    change_type = str(member_change.get("change_type") or "").strip().lower()
    if change_type not in VALID_CHANGE_TYPES:
        return None
    members = _normalize_members(member_change.get("members"))
    if not members:
        return None
    parsed: dict[str, Any] = {"change_type": change_type, "members": members}
    if change_type == "rename":
        parsed["new_name"] = str(member_change.get("new_room_nick_name") or "").strip()
        parsed["old_name"] = str(member_change.get("old_room_nick_name") or "").strip()
    after_count = member_change.get("after_count")
    if isinstance(after_count, int) and after_count >= 0:
        parsed["after_count"] = after_count
    return parsed


def _parse_wechat_member_change(content: str) -> dict[str, Any] | None:
    """把微信协议系统消息转换为与通用渠道一致的成员事件。"""

    if "<sysmsg" not in content:
        return None
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    if root.tag != "sysmsg" or str(root.attrib.get("type") or "").strip() != "sysmsgtemplate":
        return None
    template_node = root.find(".//content_template/template")
    template_text = str(template_node.text or "").strip() if template_node is not None else ""
    if any(pattern in template_text for pattern in JOIN_PATTERNS):
        link_names = ("names", "adder")
        change_type = "join"
    elif any(pattern in template_text for pattern in REMOVED_PATTERNS):
        link_names = ("names", "username")
        change_type = "removed"
    elif any(pattern in template_text for pattern in LEAVE_PATTERNS):
        link_names = ("username", "names")
        change_type = "left"
    else:
        return None
    for link_name in link_names:
        members = _parse_wechat_link_members(root, link_name)
        if members:
            return {"change_type": change_type, "members": members}
    return None


def _parse_wechat_link_members(root: ET.Element, link_name: str) -> list[dict[str, str]]:
    """读取微信系统消息中的 memberlist 或单 member 结构。"""

    link = root.find(f".//link[@name='{link_name}']")
    if link is None:
        return []
    nodes = link.findall("memberlist/member") or link.findall("member")
    members: list[dict[str, str]] = []
    for node in nodes:
        external_id = str(node.findtext("username") or "").strip()
        if not external_id:
            continue
        display_name = str(node.findtext("nickname") or "").strip() or external_id
        members.append({"external_id": external_id, "display_name": display_name})
    return members


def _normalize_members(value: Any) -> list[dict[str, str]]:
    """校验通用成员列表,只保留平台定义的公开字段。"""

    if not isinstance(value, (list, tuple)):
        return []
    members: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        external_id = str(item.get("external_id") or "").strip()
        if not external_id or external_id in seen:
            continue
        seen.add(external_id)
        display_name = str(item.get("display_name") or "").strip() or external_id
        members.append({"external_id": external_id, "display_name": display_name})
    return members


def _find_member(db: Session, *, chatroom: Chatroom, external_id: str) -> ChatroomMember | None:
    return db.scalar(
        select(ChatroomMember).where(
            ChatroomMember.tenant_id == int(chatroom.tenant_id),
            ChatroomMember.chatroom_id == int(chatroom.id),
            ChatroomMember.member_external_id == external_id,
        )
    )


def apply_member_change(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    change_type: str,
    members: list[dict[str, str]],
    new_name: str = "",
) -> int:
    """在当前租户和当前群内更新成员,退出与移除统一进入 left 状态;rename 只改群内昵称。"""

    current = now_shanghai()
    updated_count = 0
    for item in members:
        external_id = item["external_id"]
        display_name = item["display_name"]
        member = _find_member(db, chatroom=chatroom, external_id=external_id)
        if member is None:
            member = ChatroomMember(
                tenant_id=int(chatroom.tenant_id),
                chatroom_id=int(chatroom.id),
                bot_id=int(channel.bot_id),
                channel_id=int(channel.id),
                member_external_id=external_id,
            )
            db.add(member)
        member.last_synced_at = current
        if change_type == "rename":
            # 群昵称变更:只更新群内昵称,不改变在群状态,也不动全局昵称。
            member.display_name = new_name or member.nickname or display_name
            updated_count += 1
            continue
        member.nickname = display_name
        member.display_name = display_name
        member.status = "active" if change_type == "join" else "left"
        updated_count += 1
    if updated_count:
        db.flush()
    return updated_count


def lookup_previous_names(
    db: Session,
    *,
    chatroom: Chatroom,
    members: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    """改群昵称前读取成员当前的全局昵称和群昵称,用于通知里的“旧 → 新”。"""

    result: dict[str, dict[str, str]] = {}
    for item in members:
        member = _find_member(db, chatroom=chatroom, external_id=item["external_id"])
        if member is not None:
            result[item["external_id"]] = {
                "nickname": str(member.nickname or "").strip(),
                "display_name": str(member.display_name or "").strip(),
            }
    return result


def build_reply_text(
    change_type: str,
    members: list[dict[str, str]],
    *,
    new_name: str = "",
    old_name: str = "",
    previous: dict[str, dict[str, str]] | None = None,
) -> str:
    """群内变动通知,格式与 abot 一致:标题 + 每个变动一行,加入、退出、移除、改群昵称都通知。"""

    lines: list[str] = []
    if change_type == "join":
        lines = [f"🆕 成员新增: {item['display_name']}" for item in members if item.get("display_name")]
    elif change_type in {"left", "removed"}:
        lines = [f"❌ 成员减少: {item['display_name']}" for item in members if item.get("display_name")]
    elif change_type == "rename" and members:
        item = members[0]
        before = (previous or {}).get(item["external_id"], {})
        # 旧群昵称优先用事件自带的,其次用库里改名前记录的群昵称/昵称,都没有写“未设置”。
        old = old_name or before.get("display_name") or before.get("nickname") or "未设置"
        who = before.get("nickname") or item.get("display_name") or item["external_id"]
        lines = [f"📝 昵称修改: {who} ({old} -> {new_name or '已清除'})"]
    if not lines:
        return ""
    return "📋 群成员变动通知:\n" + "\n".join(lines)


register_handler(FEATURE, handle)
