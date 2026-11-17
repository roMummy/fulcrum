"""按消息发生时间分页，允许历史记录晚于实时消息入库。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Message


def list_history_messages(
    db: Session, conversation_id: int, *, before_id: int = 0, after_id: int = 0, limit: int = 30,
    message_ids: list[int] | None = None,
) -> tuple[list[Message], bool]:
    statement = select(Message).where(Message.conversation_id == conversation_id, Message.status != "echo")
    if message_ids is not None:
        return list(db.scalars(statement.where(Message.id.in_(message_ids)).order_by(Message.created_at.asc(), Message.id.asc())).all()), False
    order = (Message.created_at.asc(), Message.id.asc()) if after_id else (Message.created_at.desc(), Message.id.desc())
    cursor_id = after_id or before_id
    if cursor_id:
        # 游标仍使用稳定主键，排序位置取该会话中对应消息的时间和主键。
        # 不接受另一个会话的游标，也不依赖可随补录改变的行偏移。
        cursor = db.scalar(statement.where(Message.id == cursor_id))
        if cursor is None:
            return [], False
        id_boundary = Message.id > cursor.id if after_id else Message.id < cursor.id
        time_boundary = Message.created_at > cursor.created_at if after_id else Message.created_at < cursor.created_at
        # 将时间与同秒主键边界分开读取，避免 MariaDB 把 OR 边界改成全表排序。
        tied = statement.where(Message.created_at == cursor.created_at, id_boundary)
        fetched = list(db.scalars(tied.order_by(*order).limit(limit + 1)).all())
        remaining = limit + 1 - len(fetched)
        if remaining:
            fetched.extend(db.scalars(statement.where(time_boundary).order_by(*order).limit(remaining)).all())
    else:
        fetched = list(db.scalars(statement.order_by(*order).limit(limit + 1)).all())
    page = fetched[:limit]
    return (page if after_id else list(reversed(page))), len(fetched) > limit


def latest_history_messages(db: Session, conversation_ids: list[int]) -> dict[int, Message]:
    """每个会话按时间取摘要，沿用分页索引，避免扫描全部导入历史。"""
    if not conversation_ids:
        return {}
    # 每个子查询绑定明确会话，避免 MariaDB 对外层关联值使用全索引扫描。
    # 第一条 SQL 只取主键，第二条才加载消息内容，查询次数固定为两次。
    lookups = [
        select(Message.id)
        .where(Message.conversation_id == conversation_id, Message.status != "echo")
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(1)
        .scalar_subquery()
        for conversation_id in conversation_ids
    ]
    identifiers = [identifier for identifier in db.execute(select(*lookups)).one() if identifier is not None]
    if not identifiers:
        return {}
    return {int(item.conversation_id): item for item in db.scalars(select(Message).where(Message.id.in_(identifiers))).all()}
