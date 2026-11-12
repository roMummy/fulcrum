"""插件取群成员昵称:排行榜、点名等需要“把 wxid 显示成人名”的场景统一用这里。

为什么放在平台:消息里的 sender_name 微桥常常给不出(退化成 wxid),插件如果在记分时把它存下来,
排行榜上就永远是 wxid;而群成员表里往往已经同步到了真实群昵称。所以插件应当存稳定的用户 ID,
展示时再用本函数实时换成昵称——昵称之后更新了,排行榜也跟着更新。
"""

from __future__ import annotations

from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chatroom, ChatroomMember
from app.services.identity_directory import looks_like_raw_id


def member_names(
    db: Session,
    *,
    channel_id: int,
    group_external_id: str,
    user_ids: Iterable[str],
    fallbacks: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """批量取某个群里一批成员的显示名,返回 {用户 ID: 名字},每个传入的 ID 都有结果。

    优先级:本群群昵称/昵称 > fallbacks(例如插件当时记下的名字,若它本身不是 wxid) > 用户 ID 本身。
    只查传入的这批 ID,一次查询;db 是当前租户的会话,租户隔离由它保证。
    """

    ids = [str(item) for item in dict.fromkeys(user_ids) if str(item or "").strip()]
    if not ids:
        return {}
    rows = db.execute(
        select(ChatroomMember.member_external_id, ChatroomMember.display_name, ChatroomMember.nickname)
        .join(Chatroom, Chatroom.id == ChatroomMember.chatroom_id)
        .where(
            Chatroom.channel_id == int(channel_id),
            Chatroom.external_id == str(group_external_id),
            ChatroomMember.member_external_id.in_(ids),
        )
    ).all()
    known: dict[str, str] = {}
    for member_id, display_name, nickname in rows:
        for candidate in (display_name, nickname):
            text = str(candidate or "").strip()
            if text and not looks_like_raw_id(text):
                known[str(member_id)] = text
                break
    fallbacks = fallbacks or {}
    result: dict[str, str] = {}
    for user_id in ids:
        fallback = str(fallbacks.get(user_id) or "").strip()
        result[user_id] = known.get(user_id) or (fallback if fallback and not looks_like_raw_id(fallback) else user_id)
    return result
