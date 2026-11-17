"""群成员变更提醒:同步群成员加入、退出和群昵称变化,并按群开关在群里发送通知与欢迎卡片。

原先是 group_member_change 插件,现在属于群自身能力:不需要机器人绑定,
在群详情页「功能」里打开即可;微桥成员目录由入站事件独立更新,本功能只控制通知。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation
from app.services.group_features.dispatch import FeatureContext, register_handler
from app.services.config_fields import ConfigField
from app.services.group_features.registry import GroupFeature, register
from app.services.plugin_runtime.replies import MAX_TEXT_LENGTH, PluginReplyError, link_reply, text_reply

logger = get_logger(__name__)

# 每次事件最多给几位新成员发欢迎卡片,防止一次拉很多人时刷屏。
MAX_WELCOME_CARDS = 3
# 成员目录默认异步读取，首次入群的头像通常比事件晚几秒返回；只让原任务
# 短暂让出队列，不能占住工作线程，也不能在手机离线时无限等待。
WELCOME_AVATAR_MAX_WAIT = timedelta(seconds=15)
WELCOME_AVATAR_RECHECK = timedelta(seconds=1)
# 文字通知和欢迎卡片共用单次匹配，只替换各自上下文提供的变量。
# 不递归解释昵称或群名中的花括号，未声明变量保留给用户检查。
WELCOME_TEMPLATE_FIELDS = ("card_title", "card_desc", "card_url", "card_thumb_url")
TEMPLATE_VARIABLE = re.compile(r"\{([a-z_]+)\}")
NOTIFICATION_VARIABLE_HINT = "可用变量：{nickname} 用户昵称、{group_name} 群名称、{now} 当前时间"

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
        description="成员加入、退出、改群昵称时在群里通知，可按群自定义文案，并向新成员发送欢迎卡片。",
        event_types=("event", "system", "text", "group_member_join", "group_member_leave", "group_member_nickname_change"),
        fields=(
            ConfigField("notify_join", "新成员加入时通知", kind="bool", default=True, group="通知方式"),
            ConfigField("notify_leave", "成员退出或被移出时通知", kind="bool", default=True, group="通知方式"),
            ConfigField("notify_rename", "成员修改群昵称时通知", kind="bool", default=True, group="通知方式"),
            # 默认文案仅在声明中维护；群存储会补齐缺省值，生成通知时只使用本群生效配置。
            # 正文分别依赖通知开关，空正文仅停发对应文字，不影响已开启的欢迎卡片。
            ConfigField(
                "notify_title", "通知标题", default="📋 群成员变动通知:", group="通知文案",
                hint="留空不添加标题；" + NOTIFICATION_VARIABLE_HINT + "（标题中多人昵称用顿号连接）",
            ),
            ConfigField(
                "join_template", "新成员加入文案", kind="textarea", default="🆕 成员新增: {nickname}",
                group="通知文案", depends_on="notify_join",
                hint="留空不发加入文字，仍可发送欢迎卡片；" + NOTIFICATION_VARIABLE_HINT,
            ),
            ConfigField(
                "leave_template", "成员退出文案", kind="textarea", default="❌ 成员减少: {nickname}",
                group="通知文案", depends_on="notify_leave",
                hint="留空不发退出文字；" + NOTIFICATION_VARIABLE_HINT,
            ),
            ConfigField(
                "removed_template", "成员被移出文案", kind="textarea", default="❌ 成员减少: {nickname}",
                group="通知文案", depends_on="notify_leave",
                hint="留空不发被移出文字；" + NOTIFICATION_VARIABLE_HINT,
            ),
            ConfigField(
                "rename_template", "修改群昵称文案", kind="textarea", default="📝 昵称修改: {nickname} ({old_nickname} -> {new_nickname})",
                group="通知文案", depends_on="notify_rename",
                hint="留空不发改昵称文字；" + NOTIFICATION_VARIABLE_HINT + "、{old_nickname} 原群昵称、{new_nickname} 新群昵称",
            ),
            ConfigField("card_enabled", "新成员加入时发送欢迎卡片", kind="bool", default=False, group="欢迎卡片"),
            ConfigField(
                "card_title", "卡片标题", default="欢迎 {nickname} 加入本群", group="欢迎卡片", depends_on="card_enabled",
                hint="可用变量:{nickname} 新成员昵称,{now} 当前时间,{avatar_url} 新成员头像地址",
            ),
            ConfigField("card_desc", "卡片描述", default="{now}", group="欢迎卡片", depends_on="card_enabled", hint="可用变量同上"),
            ConfigField(
                "card_url", "点击后打开的页面", group="欢迎卡片", depends_on="card_enabled",
                hint="支持上述变量;替换后必须以 http:// 或 https:// 开头;群成员手机能打开才能点开",
            ),
            ConfigField(
                "card_thumb_source", "缩略图来源", kind="select", default="url", group="欢迎卡片", depends_on="card_enabled",
                options=(("url", "自定义图片 URL"), ("member_avatar", "新成员头像")),
                hint="选择新成员头像后自动使用该新人的已同步微信头像，无需填写地址；未获取头像时不带缩略图",
            ),
            ConfigField(
                "card_thumb_url", "自定义图片 URL", group="欢迎卡片", depends_on="card_enabled",
                show_when=("card_thumb_source", "url"),
                hint="填写 http:// 或 https:// 图片直链，留空不显示图片；切换来源会保留已填地址",
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
    directory = ctx.details.get("member_directory")
    if ctx.channel.channel_type == "wechat_webridge":
        # 入站事务已更新微桥目录;异步通知不能再写一次,否则会把更新后的资料回滚。
        if isinstance(directory, Mapping) and not directory.get("applied"):
            return None
        previous = dict(directory.get("previous") or {}) if isinstance(directory, Mapping) else {}
        if change_type == "join":
            # 欢迎任务让出队列期间可能又收到退群事件；发送前读取当前成员状态，
            # 避免在退出通知之后再给已经离群的人发送迟到欢迎。
            members = _current_join_members(ctx.db, ctx.chatroom, members)
            if not members:
                return None
    else:
        previous = lookup_previous_names(ctx.db, chatroom=ctx.chatroom, members=members) if change_type == "rename" else {}
        apply_member_change(
            ctx.db, channel=ctx.channel, chatroom=ctx.chatroom,
            change_type=change_type, members=members, new_name=new_name,
        )
        if isinstance(parsed.get("after_count"), int):
            ctx.chatroom.member_count = int(parsed["after_count"])

    # 下面的开关只决定要不要发消息。
    notify_key = {"join": "notify_join", "left": "notify_leave", "removed": "notify_leave", "rename": "notify_rename"}[change_type]
    if not ctx.config.get(notify_key):
        return []

    replies: list[dict[str, Any]] = []
    reply_text = build_reply_text(
        change_type, members, config=ctx.config, group_name=ctx.chatroom.name or ctx.chatroom.external_id,
        new_name=new_name, old_name=str(parsed.get("old_name") or ""), previous=previous,
    )
    if reply_text:
        replies.append(text_reply(reply_text))
    if change_type == "join" and ctx.config.get("card_enabled"):
        card_members = members[:MAX_WELCOME_CARDS]
        if _welcome_uses_avatar(ctx.config):
            card_members = _welcome_member_profiles(ctx.db, ctx.channel, ctx.chatroom, card_members)
        replies.extend(build_welcome_cards(ctx.config, card_members))
    return replies


def _welcome_uses_avatar(config: Mapping[str, Any]) -> bool:
    """等待和最终生成共用同一判断，头像模式不检查已隐藏的自定义地址。"""

    use_member_avatar = config.get("card_thumb_source") == "member_avatar"
    fields = WELCOME_TEMPLATE_FIELDS[:-1] if use_member_avatar else WELCOME_TEMPLATE_FIELDS
    return use_member_avatar or any("{avatar_url}" in str(config.get(field) or "") for field in fields)


def _current_join_members(db: Session, room: Chatroom, members: list[dict[str, str]]) -> list[dict[str, str]]:
    """只排除本群已经明确离群的成员，等待判断和最终欢迎使用同一名单。"""

    left_ids = set(db.scalars(select(ChatroomMember.member_external_id).where(
        ChatroomMember.chatroom_id == room.id, ChatroomMember.status == "left",
        ChatroomMember.member_external_id.in_([item["external_id"] for item in members]),
    )).all())
    return [item for item in members if item["external_id"] not in left_ids]


def _welcome_member_profiles(db: Session, channel: Channel, room: Chatroom, members: list[dict[str, str]]) -> list[dict[str, str]]:
    """按新人 ID 批量读取本群资料，同渠道好友可补缺，不读取邀请人或其他群头像。"""

    from app.services.identity_directory import IdentityDirectory

    directory = IdentityDirectory(db)
    group_id = str(room.external_id)
    directory.preload(channel.id, [item["external_id"] for item in members], group_external_id=group_id)
    return [{**item, "avatar_url": directory.get(channel.id, item["external_id"], group_external_id=group_id)["avatar_url"]}
        for item in members]


def _welcome_avatar_url(value: Any) -> str:
    """头像尚未返回或地址非法时按缺失处理，等待与卡片生成使用同一合法性规则。"""

    avatar_url = str(value or "").strip()
    try:
        avatar = urlsplit(avatar_url)
        if avatar.scheme.lower() in {"http", "https"} and avatar.hostname and avatar.username is None and avatar.password is None:
            return avatar_url
    except ValueError:
        pass
    return ""


def welcome_avatar_retry_at(
    db: Session, payload: Mapping[str, Any], *, queued_at: datetime, now: datetime | None = None,
) -> datetime | None:
    """首次欢迎缺头像且本群目录仍在同步时，返回原任务下次领取时间。

    这里只读当前配置和资料；成员目录已由入站事务申请刷新，不再下发读取任务。
    等待截止时间固定取原任务创建时间，重试或进程重启不能延长十五秒上限。
    """

    from app.services.group_features.store import get_state
    from app.services.inbound_policy import expired_message_dispatch
    from app.services.self_sent import is_self_sent
    from app.services.wechat_webridge.member_directory import state as directory_state

    event = payload.get("normalized_event")
    if not isinstance(event, Mapping):
        return None
    details = event.get("details") or {}
    if not isinstance(details, Mapping) or details.get("skip_plugin_dispatch") or is_self_sent(event):
        return None
    current = ensure_shanghai_datetime(now or now_shanghai())
    if expired_message_dispatch(event, now=current) is not None:
        return None
    applied = details.get("member_directory")
    if not isinstance(applied, Mapping) or not applied.get("applied"):
        return None
    parsed = parse_member_change(event)
    if parsed is None or parsed["change_type"] != "join":
        return None
    channel = db.get(Channel, int(payload.get("channel_id") or 0))
    conversation = db.get(Conversation, int(payload.get("conversation_id") or 0))
    if channel is None or channel.channel_type != "wechat_webridge" or conversation is None:
        return None
    if conversation.channel_id != channel.id or conversation.chat_type != "group":
        return None
    room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == conversation.external_id))
    if room is None or not room.is_active or room.management_status == "archived":
        return None
    enabled, config = get_state(room, FEATURE)
    if not enabled or not config.get("notify_join") or not config.get("card_enabled") or not _welcome_uses_avatar(config):
        return None
    if not directory_state(room).get("dirty"):
        return None
    members = _current_join_members(db, room, list(parsed["members"]))[:MAX_WELCOME_CARDS]
    members = _welcome_member_profiles(db, channel, room, members) if members else []
    missing = sum(not _welcome_avatar_url(item.get("avatar_url")) for item in members)
    if not missing:
        return None
    deadline = ensure_shanghai_datetime(queued_at) + WELCOME_AVATAR_MAX_WAIT
    if current >= deadline:
        logger.info("欢迎卡片头像等待到期，继续按当前资料发送", extra={"chatroom_id": room.id, "missing_avatars": missing})
        return None
    return min(current + WELCOME_AVATAR_RECHECK, deadline)


def _render_template(template: Any, variables: Mapping[str, str]) -> str:
    """按当前通知或卡片上下文替换一次，缺少变量保持原文，不执行替换结果。"""

    return TEMPLATE_VARIABLE.sub(lambda match: variables.get(match.group(1), match.group(0)), str(template or ""))


def build_welcome_cards(config: Mapping[str, Any], members: list[dict[str, str]]) -> list[dict[str, Any]]:
    """为新成员生成欢迎卡片;地址不合法时只记日志并跳过,不影响文字通知。"""

    now_text = now_shanghai().strftime("%Y-%m-%d %H:%M")
    cards: list[dict[str, Any]] = []
    for item in members[:MAX_WELCOME_CARDS]:
        nickname = item.get("display_name") or item.get("external_id") or ""
        avatar_url = _welcome_avatar_url(item.get("avatar_url"))
        variables = {"nickname": nickname, "now": now_text, "avatar_url": avatar_url}

        try:
            # 缩略图严格按显式来源选择。头像缺失时不改用已保存的自定义 URL，
            # URL 模式则继续复用同一模板替换入口，避免出现两套发送逻辑。
            thumb_url = avatar_url if config.get("card_thumb_source") == "member_avatar" else _render_template(config.get("card_thumb_url"), variables)
            cards.append(
                link_reply(
                    url=_render_template(config.get("card_url"), variables),
                    title=_render_template(config.get("card_title"), variables),
                    description=_render_template(config.get("card_desc"), variables),
                    thumb_url=thumb_url,
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
    if isinstance(after_count, int) and not isinstance(after_count, bool) and after_count >= 0:
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
    config: Mapping[str, Any],
    group_name: str,
    new_name: str = "",
    old_name: str = "",
    previous: dict[str, dict[str, str]] | None = None,
) -> str:
    """按本群模板逐人生成正文，再合并为一条通知；空正文不发送孤立标题。"""

    template_key = {"join": "join_template", "left": "leave_template", "removed": "removed_template", "rename": "rename_template"}.get(change_type)
    template = str(config.get(template_key) or "").strip() if template_key else ""
    if not template:
        return ""

    common = {"group_name": group_name, "now": now_shanghai().strftime("%Y-%m-%d %H:%M")}
    lines: list[str] = []
    nicknames: list[str] = []
    # 改昵称事件仍只描述该次变更的单个成员，不能把同一新旧昵称套用到其他成员。
    for item in members[:1] if change_type == "rename" else members:
        nickname = item.get("display_name") or item["external_id"]
        variables = {**common, "nickname": nickname}
        if change_type == "rename":
            before = (previous or {}).get(item["external_id"], {})
            # 入站目录已更新，必须消费事件保存的改名前资料；旧群昵称优先取事件的明确证据。
            nickname = before.get("nickname") or nickname
            variables.update(
                nickname=nickname,
                old_nickname=old_name or before.get("display_name") or before.get("nickname") or "未设置",
                new_nickname=new_name or "已清除",
            )
        body = _render_template(template, variables).strip()
        if body:
            lines.append(body)
            nicknames.append(nickname)
    if not lines:
        return ""

    # 标题只渲染一次，多人昵称以顿号连接；正文的内部换行按用户填写保留。
    title = _render_template(config.get("notify_title"), {**common, "nickname": "、".join(nicknames)}).strip()
    text = "\n".join(([title] if title else []) + lines)
    if len(text) > MAX_TEXT_LENGTH:
        # 多人长模板可能超过公共单条消息限制，明确提示截断，避免抛出异常后整次卡片也丢失。
        suffix = "\n…（通知内容过长，已截断）"
        logger.warning("群成员通知内容超过单条上限，已截断", extra={"change_type": change_type, "member_count": len(members), "text_length": len(text)})
        text = text[:MAX_TEXT_LENGTH - len(suffix)].rstrip() + suffix
    return text


register_handler(FEATURE, handle)
