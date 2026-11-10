"""WeBridge 群详情页数据。

群资料、成员快照、消息流水的组装逻辑与企微群详情一致，直接复用其构建函数，
这里只覆盖微桥特有的同步入口、文案和不支持的能力开关。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, Message, PluginExecutionLog
from app.services.plugin_schemas import parse_config_json
from app.services.points import PointsService
from app.services.workwechat_chatrooms import build_workwechat_chatroom_detail


def _relative_days_label(last_active, current) -> str:
    """把最后发言时间转成“今天 / N天前”;没有记录返回“暂无记录”。"""

    if last_active is None:
        return "暂无记录"
    days = max((current - ensure_shanghai_datetime(last_active)).days, 0)
    return "今天" if days == 0 else f"{days} 天前"


def _build_health(db: Session, channel: Channel, chatroom: Chatroom, members: list[ChatroomMember]) -> dict[str, Any]:
    """群健康度:口径参照 abot(沉默占比、30 天活跃覆盖、30 天消息量、插件使用)。

    只统计仍在群内的成员;沉默 = 30 天内没有发言记录(含没有记录)。
    """

    current = now_shanghai()
    t7, t30 = current - timedelta(days=7), current - timedelta(days=30)
    total = active_7d = active_30d = 0
    for member in members:
        if member.status == "left":
            continue
        total += 1
        if member.last_active_at is None:
            continue
        last = ensure_shanghai_datetime(member.last_active_at)
        active_7d += 1 if last >= t7 else 0
        active_30d += 1 if last >= t30 else 0
    silent = total - active_30d

    conversation_id = db.scalar(
        select(Conversation.id).where(Conversation.channel_id == channel.id, Conversation.external_id == chatroom.external_id)
    )
    msg_30d = 0
    plugin_calls_30d = 0
    if conversation_id:
        msg_30d = int(
            db.scalar(select(func.count(Message.id)).where(Message.conversation_id == conversation_id, Message.created_at >= t30)) or 0
        )
        plugin_calls_30d = int(
            db.scalar(
                select(func.count(PluginExecutionLog.id)).where(
                    PluginExecutionLog.conversation_id == conversation_id,
                    PluginExecutionLog.attempted.is_(True),
                    PluginExecutionLog.created_at >= t30,
                )
            )
            or 0
        )

    score = 100
    if total > 0:
        score -= min(35, round(silent / total * 35))
        score -= min(25, round((1 - min(active_30d / total, 1)) * 25))
    if msg_30d == 0:
        score -= 25
    elif msg_30d < 50:
        score -= 12
    if plugin_calls_30d == 0:
        score -= 10
    score = max(0, min(100, score))
    return {
        "score": score,
        "level": "良好" if score >= 80 else ("一般" if score >= 60 else "偏低"),
        "total": total,
        "active_7d": active_7d,
        "active_30d": active_30d,
        "silent": silent,
        "messages_30d": msg_30d,
        "plugin_calls_30d": plugin_calls_30d,
    }


def build_webridge_chatroom_detail(db: Session, channel: Channel, chatroom: Chatroom) -> dict[str, Any]:
    """返回微桥群详情；成员同步通过 room_members 命令下发给手机插件。"""

    detail = build_workwechat_chatroom_detail(db, channel, chatroom)
    # 成员积分:本群账户余额,与插件消费/赚取使用同一份账户。
    balances = PointsService(db).group_balances(str(chatroom.external_id))
    rows = db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id)).all()
    last_map = {row.member_external_id: row.last_active_at for row in rows}
    current = now_shanghai()
    for member in detail.get("members", []):
        member["points"] = balances.get(str(member.get("member_external_id")), 0)
        member["last_active_label"] = _relative_days_label(last_map.get(member.get("member_external_id")), current)
    # 成员按最后发言倒序,没有记录的排最后,方便一眼看到最近活跃和沉默的人。
    detail["members"].sort(
        key=lambda m: ensure_shanghai_datetime(last_map[m["member_external_id"]]).timestamp()
        if last_map.get(m["member_external_id"])
        else 0,
        reverse=True,
    )
    detail["health"] = _build_health(db, channel, chatroom, rows)
    detail.update(
        {
            "show_points": True,
            # 同步按钮走通用单群同步入口，该入口会为微桥渠道入队 room_members 命令。
            "sync_action": f"/channels/{channel.id}/chatrooms/{chatroom.id}/sync",
            "sync_button_label": "同步群成员",
            "member_section_hint": "成员来自手机插件的群成员同步；为空时点击右上角“同步群成员”，稍后刷新本页。",
            # 默认关闭:手机端回传媒体会占用串行通道、拖慢消息上报,需要时再按群开启。
        }
    )
    return detail
