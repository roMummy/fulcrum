from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime
from app.models.entities import BotPlugin, Conversation, Message
from app.services.plugin_runtime.contracts import PluginExecutionContext


MAX_PLUGIN_MESSAGE_LIMIT = 2000


class PluginMessageAccessError(ValueError):
    """插件消息查询缺少可信定位信息，或目标资源不属于当前执行上下文。"""


@dataclass(frozen=True)
class PluginMessageRecord:
    """传给插件的最小消息记录，不暴露 ORM 对象和渠道原始载荷。"""

    message_id: int
    created_at: datetime
    direction: str
    sender_id: str | None
    sender_name: str
    content: str
    formatted_text: str


@dataclass(frozen=True)
class PluginMessageBatch:
    """一次受控消息读取的会话信息与按时间排序结果。"""

    conversation_id: int
    conversation_title: str
    channel_type: str
    start_at: datetime
    end_at: datetime
    messages: tuple[PluginMessageRecord, ...]

    @property
    def total(self) -> int:
        return len(self.messages)


class PluginMessageReader:
    """依据平台可信执行上下文读取当前租户的群聊文本消息。"""

    def __init__(self, db: Session, context: PluginExecutionContext):
        self.db = db
        self.context = context

    def read(self, *, start_at: datetime, end_at: datetime, limit: int) -> PluginMessageBatch:
        """校验完整归属后读取时间范围内最近的文本消息，并按时间正序返回。"""

        tenant_id = require_tenant_id()
        if int(self.context.tenant_id) != tenant_id:
            raise PluginMessageAccessError("插件执行租户与当前租户会话不一致")
        normalized_start = ensure_shanghai_datetime(start_at)
        normalized_end = ensure_shanghai_datetime(end_at)
        if normalized_end <= normalized_start:
            raise PluginMessageAccessError("消息查询时间范围不合法")
        normalized_limit = _normalize_limit(limit)

        binding = self.db.get(BotPlugin, int(self.context.binding_id))
        if binding is None:
            raise PluginMessageAccessError("插件绑定不存在或不属于当前租户")
        if int(binding.bot_id) != int(self.context.bot_id):
            raise PluginMessageAccessError("插件绑定与执行机器人不一致")

        conversation_id = _require_positive_id(self.context.conversation_id, "插件执行上下文缺少目标会话")
        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            raise PluginMessageAccessError("目标会话不存在或不属于当前租户")
        if int(conversation.bot_id) != int(binding.bot_id):
            raise PluginMessageAccessError("目标会话不属于当前插件机器人")
        if self.context.channel_id is not None and int(conversation.channel_id or 0) != int(self.context.channel_id):
            raise PluginMessageAccessError("目标会话不属于当前插件渠道")
        if str(conversation.chat_type or "").strip().lower() != "group":
            raise PluginMessageAccessError("消息总结只允许读取群聊会话")

        # 先按时间倒序限制数据库返回量，再在内存中恢复正序。这样大型群聊只读取
        # 指定窗口内最近的有限消息，同时使用主键作为同秒消息的稳定排序依据。
        rows = self.db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.created_at >= normalized_start,
                Message.created_at < normalized_end,
                Message.message_type == "text",
                func.trim(Message.content) != "",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(normalized_limit)
        ).all()
        records = tuple(_to_record(row) for row in reversed(rows))
        return PluginMessageBatch(
            conversation_id=conversation_id,
            conversation_title=str(conversation.title or conversation.external_id or "群聊").strip() or "群聊",
            channel_type=str(conversation.channel_type or self.context.channel_type or "").strip(),
            start_at=normalized_start,
            end_at=normalized_end,
            messages=records,
        )


def _to_record(message: Message) -> PluginMessageRecord:
    """把数据库消息压缩为业务插件可使用的稳定文本结构。"""

    content = re.sub(r"\s+", " ", str(message.content or "")).strip()
    sender_name = str(message.sender_name or message.sender_id or "").strip()
    if not sender_name:
        sender_name = "机器人" if str(message.direction or "").strip().lower() == "out" else "成员"
    return PluginMessageRecord(
        message_id=int(message.id),
        created_at=ensure_shanghai_datetime(message.created_at),
        direction=str(message.direction or "").strip(),
        sender_id=str(message.sender_id).strip() if message.sender_id else None,
        sender_name=sender_name,
        content=content,
        formatted_text=f"{sender_name}: {content}",
    )


def _normalize_limit(limit: int) -> int:
    """拒绝模糊或过大的查询量，避免插件意外扫描整个会话历史。"""

    if isinstance(limit, bool):
        raise PluginMessageAccessError("消息查询数量必须是正整数")
    try:
        normalized = int(limit)
    except (TypeError, ValueError) as exc:
        raise PluginMessageAccessError("消息查询数量必须是正整数") from exc
    if normalized <= 0:
        raise PluginMessageAccessError("消息查询数量必须是正整数")
    if normalized > MAX_PLUGIN_MESSAGE_LIMIT:
        raise PluginMessageAccessError(f"单次最多读取 {MAX_PLUGIN_MESSAGE_LIMIT} 条消息")
    return normalized


def _require_positive_id(value: int | None, message: str) -> int:
    """可信上下文中的资源编号仍需做类型和正数校验。"""

    if isinstance(value, bool):
        raise PluginMessageAccessError(message)
    try:
        normalized = int(value) if value is not None else 0
    except (TypeError, ValueError) as exc:
        raise PluginMessageAccessError(message) from exc
    if normalized <= 0:
        raise PluginMessageAccessError(message)
    return normalized

