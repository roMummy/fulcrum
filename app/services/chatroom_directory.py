"""群与群成员的"查或建":并发安全的统一入口。

为什么需要它:每个渠道都是"先查,没有就 new 一个再 flush"。两个请求同时处理同一个新群的首批消息时,
都查到"不存在",都插入,结果同一个群出现多条记录。数据库里现在有唯一约束兜底
(chatrooms: channel_id+external_id;chatroom_members: chatroom_id+member_external_id),
但约束只会让第二个插入报错;这里负责把这个报错变成"回读对方刚建的那条"。

做法:
1. 先用加锁读(SELECT ... FOR UPDATE)查——加锁读读到的是最新已提交的数据,而 MariaDB 默认可重复读下的普通读
   看不到别的事务刚提交的行,所以回读必须是加锁读;
2. 没有就在 SAVEPOINT 里插入,只回滚这一次插入,不影响调用方事务里已经做的其他事;
3. 撞唯一键就回滚到保存点,再加锁读一次,返回已存在的那条,并告诉调用方"不是我建的"(created=False)——
   调用方据此决定是否执行"首次发现"的初始化,冲突方不应该重复初始化。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.entities import Channel, Chatroom, ChatroomMember


def _find_room(db: Session, channel_id: int, external_id: str) -> Chatroom | None:
    # 项目的会话关闭了 autoflush。populate_existing 会用数据库里的值覆盖会话里的对象,
    # 所以读之前必须先 flush,否则本事务里还没写入的修改(例如刚改的群功能开关)会被旧值冲掉。
    db.flush()
    # populate_existing:确保拿到的是数据库里的最新值,而不是会话里可能过期的缓存对象
    return db.scalar(
        select(Chatroom)
        .where(Chatroom.channel_id == channel_id, Chatroom.external_id == external_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def get_or_create_chatroom(db: Session, channel: Channel, external_id: str, **defaults: Any) -> tuple[Chatroom, bool]:
    """返回 (群记录, 是否由本次调用新建)。defaults 只在新建时使用。"""

    room = _find_room(db, int(channel.id), external_id)
    if room is not None:
        return room, False
    try:
        with db.begin_nested():
            room = Chatroom(bot_id=channel.bot_id, channel_id=channel.id, external_id=external_id, **defaults)
            if getattr(room, "tenant_id", None) is None and getattr(channel, "tenant_id", None) is not None:
                room.tenant_id = channel.tenant_id
            db.add(room)
            db.flush()
        return room, True
    except IntegrityError:
        # 别的请求刚建了同一个群:读它的那条,不要再建
        existing = _find_room(db, int(channel.id), external_id)
        if existing is None:
            raise
        return existing, False


def get_or_create_member(db: Session, channel: Channel, chatroom: Chatroom, member_external_id: str, **defaults: Any) -> tuple[ChatroomMember, bool]:
    """返回 (成员记录, 是否由本次调用新建)。"""

    def find() -> ChatroomMember | None:
        db.flush()  # 同 _find_room:先写出本事务的未提交修改,再用数据库值刷新对象
        return db.scalar(
            select(ChatroomMember)
            .where(ChatroomMember.chatroom_id == chatroom.id, ChatroomMember.member_external_id == member_external_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    member = find()
    if member is not None:
        return member, False
    try:
        with db.begin_nested():
            member = ChatroomMember(
                chatroom_id=chatroom.id, bot_id=channel.bot_id, channel_id=channel.id, member_external_id=member_external_id, **defaults
            )
            if getattr(member, "tenant_id", None) is None and getattr(channel, "tenant_id", None) is not None:
                member.tenant_id = channel.tenant_id
            db.add(member)
            db.flush()
        return member, True
    except IntegrityError:
        existing = find()
        if existing is None:
            raise
        return existing, False
