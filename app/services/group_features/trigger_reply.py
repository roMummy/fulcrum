"""触发回复:群管理员在群里配置「什么时候 → 回复什么」(拍一拍、关键词…),不需要写代码。

和其他群功能的区别:开关在群功能列表里,具体规则在「管理触发规则」页(规则数量不定、含素材上传,
放不进单个配置卡)。触发和动作的种类由 app/services/trigger_rules/catalog.py 注册,
新增触发或动作不需要改这个文件。
"""

from __future__ import annotations

from typing import Any

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
    )
    return execute_rules(ctx.db, tenant_id=int(ctx.channel.tenant_id), chatroom_id=int(ctx.chatroom.id), view=view)


register_handler(FEATURE, handle)
