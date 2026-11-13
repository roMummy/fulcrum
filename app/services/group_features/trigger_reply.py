"""触发回复:群管理员在群里配置「什么时候 → 回复什么」(拍一拍、关键词…),不需要写代码。

和其他群功能的区别:开关在群功能列表里,具体规则在「管理触发规则」页(规则数量不定、含素材上传,
放不进单个配置卡)。触发和动作的种类由 app/services/trigger_rules/catalog.py 注册,
新增触发或动作不需要改这个文件。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.models.entities import ChatroomMember

from app.services.group_features.dispatch import FeatureContext, register_handler
from app.services.group_features.member_change import parse_member_change
from app.services.group_features.registry import GroupFeature, register
from app.services.trigger_rules.catalog import EventView, trigger_event_types
from app.services.trigger_rules.engine import execute_rules

FEATURE = register(
    GroupFeature(
        key="trigger_reply",
        name="触发回复",
        description="群里发生拍一拍、有人入群退群、说了某句话时,自动回复文字、图片、语音、视频或卡片。",
        event_types=trigger_event_types(),
    )
)


def bot_names(db, channel, chatroom) -> tuple[str, ...]:
    """机器人自己在这个群里的各种称呼:账号昵称、它在本群的群昵称/昵称、微信号。

    拍一拍的文案里,被拍的人显示的是他的昵称(机器人被拍时就是机器人的昵称),据此判断"拍的是不是机器人"。
    各渠道把账号 ID 和资料放在运行信息的不同键下,这里统一收集;取不到的就不放,不报错。
    """

    try:
        runtime = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError):
        runtime = {}
    runtime = runtime if isinstance(runtime, dict) else {}
    profile = runtime.get("account_profile") if isinstance(runtime.get("account_profile"), dict) else {}
    own_ids: list[str] = []
    for key in ("webridge", "guga_agent", "account_connection"):
        section = runtime.get(key) if isinstance(runtime.get(key), dict) else {}
        own_ids.append(str(section.get("account_id") or "").strip())
    own_ids.append(str(profile.get("account_id") or "").strip())
    own_ids = [item for item in dict.fromkeys(own_ids) if item]
    names = [str(profile.get("nickname") or "").strip(), str(profile.get("name") or "").strip()]
    connection = runtime.get("account_connection") if isinstance(runtime.get("account_connection"), dict) else {}
    names.append(str(connection.get("display_name") or "").strip())
    if own_ids:
        for display_name, nickname in db.execute(
            select(ChatroomMember.display_name, ChatroomMember.nickname).where(
                ChatroomMember.chatroom_id == chatroom.id, ChatroomMember.member_external_id.in_(own_ids)
            )
        ).all():
            names += [str(display_name or "").strip(), str(nickname or "").strip()]
    names += own_ids
    return tuple(dict.fromkeys(name for name in names if name))


def handle(ctx: FeatureContext, args: str) -> list[dict[str, Any]] | None:
    """没有规则命中返回 None(与本功能无关,不记入已执行);命中则返回要发送的回复。"""

    message = ctx.message
    view = EventView(
        message_type=str(message.get("message_type") or "text"),
        text=ctx.text,
        sender_id=str(message.get("sender_id") or ""),
        sender_name=str(message.get("sender_name") or ""),
        group_name=str(ctx.chatroom.name or ctx.conversation.title or ""),
        details=ctx.details,
        # 入群/退群/改群昵称:复用成员变更提醒已有的解析(结构化事件和系统消息文字都认)
        member_change=parse_member_change(ctx.normalized_event),
        self_names=bot_names(ctx.db, ctx.channel, ctx.chatroom),
    )
    return execute_rules(ctx.db, tenant_id=int(ctx.channel.tenant_id), chatroom_id=int(ctx.chatroom.id), view=view)


register_handler(FEATURE, handle)
