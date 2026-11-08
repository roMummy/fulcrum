"""WeBridge 渠道后台的本地观测视图。

这里优先展示服务端已经确认过的联系人、群组和消息；主动刷新好友或群成员仍通过
能力目录里的通用命令桥执行，避免把未确认的手机缓存伪装成本地事实。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Chatroom, ChatroomMember, Channel, Conversation, Message


def build_webridge_dashboard(db: Session, channel: Channel, *, message_limit: int = 40) -> dict:
    """构建单个 WeBridge 渠道的好友、群组和消息总览。"""

    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.channel_id == channel.id)
        .order_by(Conversation.last_message_at.desc().nullslast(), Conversation.id.desc())
    ).all()
    direct = [item for item in conversations if str(item.chat_type or "direct") == "direct"]
    groups = [item for item in conversations if str(item.chat_type or "") == "group"]
    chatrooms = db.scalars(select(Chatroom).where(Chatroom.channel_id == channel.id).order_by(Chatroom.id.desc())).all()
    members_by_chatroom = {
        int(chatroom.id): db.scalars(
            select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id).order_by(ChatroomMember.id.asc())
        ).all()
        for chatroom in chatrooms
    }
    messages = db.scalars(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.channel_id == channel.id)
        .order_by(Message.id.desc())
        .limit(max(1, min(int(message_limit or 40), 100)))
    ).all()
    conversation_map = {int(item.id): item for item in conversations}
    return {
        "direct": direct,
        "groups": groups,
        "chatrooms": chatrooms,
        "members_by_chatroom": members_by_chatroom,
        "messages": messages,
        "conversation_map": conversation_map,
        "counts": {
            "conversations": len(conversations),
            "direct": len(direct),
            "groups": len(groups),
            "chatrooms": len(chatrooms),
            "messages": int(
                db.scalar(
                    select(func.count(Message.id))
                    .join(Conversation, Message.conversation_id == Conversation.id)
                    .where(Conversation.channel_id == channel.id)
                )
                or 0
            ),
        },
    }
