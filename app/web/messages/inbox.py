"""消息中心按接入账号选择会话。"""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Bot, Channel, Conversation
from app.services.account_identity import account_identity
from app.services.conversation_kinds import apply_kind_filter


UNBOUND = -1


def account_scope(db: Session, bot_id: int, channel_id: int, conversation_id: int = 0, *, include_accounts: bool = True):
    live_bots = select(Bot.id).where(Bot.deleted_at.is_(None))
    if bot_id:
        live_bots = live_bots.where(Bot.id == bot_id)
    channel_query = select(Channel).where(Channel.deleted_at.is_(None), Channel.bot_id.in_(live_bots))
    if not include_accounts and channel_id > 0:
        channel = db.scalar(channel_query.where(Channel.id == channel_id))
        if channel is None:
            raise HTTPException(404, "账号不存在")
        return {}, channel_id
    channels = db.scalars(channel_query.order_by(Channel.id)).all()
    accounts = {channel.id: account_identity(channel) for channel in channels}
    if db.scalar(select(Conversation.id).where(Conversation.channel_id.is_(None), Conversation.bot_id.in_(live_bots)).limit(1)):
        accounts[UNBOUND] = {"name": "未绑定账号", "initial": "未"}
    if conversation_id:
        selected = db.get(Conversation, conversation_id)
        if selected is None or (bot_id and selected.bot_id != bot_id) or (channel_id and channel_id != (selected.channel_id or UNBOUND)):
            raise HTTPException(404, "该账号下没有此会话")
        channel_id = selected.channel_id or UNBOUND
    if channel_id and channel_id not in accounts:
        raise HTTPException(404, "账号不存在")
    channel_id = channel_id or next(iter(accounts), 0)
    return accounts, channel_id


def inbox_query(bot_id: int, channel_id: int, chat_type: str):
    scope = select(Conversation).where(
        Conversation.channel_id == (None if channel_id == UNBOUND else channel_id),
        Conversation.bot_id.in_(select(Bot.id).where(Bot.deleted_at.is_(None))),
    )
    if bot_id:
        scope = scope.where(Conversation.bot_id == bot_id)
    return apply_kind_filter(scope, chat_type)
