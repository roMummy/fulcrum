"""群聊记忆批量整理:把群里没有 @ 机器人的普通聊天,按批整理进长期记忆。

只对群里开了「记住群里所有聊天」的群生效。流程:
- 数据来自本来就存着的 messages 表,不新增表;"整理到哪条"的游标存在群记录 extra_json 的 ai_memory_cursor 里。
- 开启后从当前最新一条消息开始记,不回溯历史。
- 触发条件:有效消息攒满 BATCH_SIZE 条,或最后一条有效消息之后空闲 IDLE_SECONDS 秒。
- 有效消息:群里的入站文本,排除命令(/ 或 # 开头)、卡片/XML、过短的闲话。机器人自己的消息是出站,天然不在其中。
- 一批作为一份"群聊记录"写入(每行"成员微信ID: 内容",不打个人标签;记忆里不出现昵称,昵称可能带误导性,微信 ID 稳定、事后可对账),这样谁说了什么、对话上下文都保留,
  召回时任何人提问都能用到群里的共同背景。
- 每个群每天最多整理 MAX_FLUSHES_PER_DAY 批,防止刷屏时成本失控。
- 网络写入在数据库会话之外进行;写入失败不推进游标,下一轮重试(断路保护见 ai_memory)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Chatroom, Conversation, Message
from app.services import ai_memory

logger = get_logger(__name__)

# 有效消息攒满这么多条就整理一批。
BATCH_SIZE = 30
# 最后一条有效消息之后空闲这么久(秒)也整理一批。
IDLE_SECONDS = 20 * 60
# 单批最多包含的消息条数与总字数;超出的留给下一批。
MAX_BATCH = 60
MAX_BATCH_CHARS = 8000
LINE_CHARS = 300
# 一次最多扫描多少条新消息。
SCAN_LIMIT = 500
# 每个群每天最多整理的批数。
MAX_FLUSHES_PER_DAY = 60
CURSOR_KEY = "ai_memory_cursor"
# 过短的发言不记。
MIN_CHARS = ai_memory.MIN_REMEMBER_CHARS

_SENDER_PREFIX = re.compile(r"^wxid_[0-9a-z]+:\s*\n", re.IGNORECASE)


@dataclass(frozen=True)
class DigestPlan:
    """一批待写入的群聊记录,以及写入成功后游标要推进到哪里。"""

    chatroom_id: int
    bank_id: str
    bank_name: str
    content: str
    when: datetime
    count: int
    cursor_message_id: int


def clean_text(content: object) -> str:
    """去掉"wxid_xxx:\\n"这类发送人前缀并压成单行;卡片/XML 返回空串。"""

    raw = _SENDER_PREFIX.sub("", str(content or "").strip())
    if "<?xml" in raw or raw.startswith("<msg") or "<appmsg" in raw:
        return ""
    return re.sub(r"\s+", " ", raw).strip()


def is_eligible(content: object, message_type: str = "text") -> bool:
    if message_type != "text":
        return False
    text = clean_text(content)
    return len(text) >= MIN_CHARS and text[:1] not in "/#"


def _load_extra(chatroom: Chatroom) -> dict[str, Any]:
    try:
        value = json.loads(chatroom.extra_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_cursor(chatroom: Chatroom, cursor: dict[str, Any]) -> None:
    extra = _load_extra(chatroom)
    extra[CURSOR_KEY] = cursor
    chatroom.extra_json = json.dumps(extra, ensure_ascii=False, sort_keys=True)


def _cursor(chatroom: Chatroom) -> dict[str, Any] | None:
    value = _load_extra(chatroom).get(CURSOR_KEY)
    return value if isinstance(value, dict) and isinstance(value.get("message_id"), int) else None


def plan_group_digest(db: Session, chatroom: Chatroom, conversation: Conversation, *, now: datetime | None = None) -> DigestPlan | None:
    """判断这个群现在该不该整理一批;该整理就返回计划,不访问网络。

    会按需更新游标(首次开启时定位到当前最新消息;没有有效消息时跳过已扫描的部分),调用方负责提交。
    """

    now = ensure_shanghai_datetime(now or now_shanghai())
    cursor = _cursor(chatroom)
    if cursor is None:
        # 首次开启:从当前最新一条消息开始记,不回溯历史。
        latest = db.scalar(select(func.max(Message.id)).where(Message.conversation_id == conversation.id)) or 0
        _save_cursor(chatroom, {"message_id": int(latest), "day": now.date().isoformat(), "flushes": 0})
        return None

    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id, Message.id > cursor["message_id"], Message.direction == "in", Message.message_type == "text")
        .order_by(Message.id.asc())
        .limit(SCAN_LIMIT)
    ).all()
    if not rows:
        return None
    # 没有发言人 ID 的消息无法归属,不记。
    eligible = [row for row in rows if row.sender_id and is_eligible(row.content)]
    if not eligible:
        # 扫到的都不是有效消息:直接跳过,下次不再重复扫描。
        _save_cursor(chatroom, {**cursor, "message_id": int(rows[-1].id)})
        return None

    last_at = ensure_shanghai_datetime(eligible[-1].created_at)
    if len(eligible) < BATCH_SIZE and now - last_at < timedelta(seconds=IDLE_SECONDS):
        return None

    today = now.date().isoformat()
    flushes = int(cursor.get("flushes") or 0) if cursor.get("day") == today else 0
    if flushes >= MAX_FLUSHES_PER_DAY:
        return None

    lines: list[str] = []
    used = 0
    included: list[Message] = []
    for row in eligible[:MAX_BATCH]:
        line = f"{ai_memory.speaker_label(str(row.sender_id))}: {clean_text(row.content)[:LINE_CHARS]}"
        if included and used + len(line) > MAX_BATCH_CHARS:
            break
        lines.append(line)
        included.append(row)
        used += len(line) + 1
    # 本批之后还有有效消息时,游标停在本批最后一条;否则可以直接跳过已扫描的全部消息。
    more_eligible = len(included) < len(eligible)
    cursor_id = int(included[-1].id) if more_eligible else int(rows[-1].id)
    return DigestPlan(
        chatroom_id=int(chatroom.id),
        bank_id=ai_memory.bank_id(
            tenant_id=int(chatroom.tenant_id), channel_id=int(chatroom.channel_id), chat_type="group", group_external_id=chatroom.external_id
        ),
        bank_name=str(chatroom.remark or chatroom.name or ""),
        content="\n".join(lines),
        when=ensure_shanghai_datetime(included[-1].created_at),
        count=len(included),
        cursor_message_id=cursor_id,
    )


def commit_flush(chatroom: Chatroom, plan: DigestPlan, *, now: datetime | None = None) -> None:
    """写入成功后推进游标并累计今天的整理批数。"""

    now = ensure_shanghai_datetime(now or now_shanghai())
    cursor = _cursor(chatroom) or {}
    today = now.date().isoformat()
    flushes = int(cursor.get("flushes") or 0) if cursor.get("day") == today else 0
    _save_cursor(chatroom, {"message_id": plan.cursor_message_id, "day": today, "flushes": flushes + 1})


def run_for_tenant(*, now: datetime | None = None) -> int:
    """扫描当前租户里开了"记住所有聊天"的群,整理到点的批次;返回成功整理的批数。

    必须在租户上下文中调用。网络写入在数据库会话之外进行。
    """

    from app.core.db import TenantSessionLocal
    from app.core.tenant_db import tenant_session
    from app.services import ai_dispatch

    if not ai_memory.is_configured():
        return 0
    plans: list[DigestPlan] = []
    with tenant_session(TenantSessionLocal) as db:
        for room in db.scalars(select(Chatroom).where(Chatroom.is_active.is_(True))).all():
            resolved = ai_dispatch.resolve_for_conversation(
                db, channel_id=room.channel_id, chat_type="group", conversation_external_id=room.external_id
            )
            if resolved is None or not (resolved.chat_enabled and resolved.memory_enabled and resolved.memory_all):
                continue
            conversation = db.scalar(
                select(Conversation).where(Conversation.channel_id == room.channel_id, Conversation.external_id == room.external_id)
            )
            if conversation is None:
                continue
            plan = plan_group_digest(db, room, conversation, now=now)
            if plan is not None:
                plans.append(plan)
        db.commit()

    flushed = 0
    for plan in plans:
        ok = ai_memory.retain_batch(plan.bank_id, content=plan.content, when=plan.when, bank_name=plan.bank_name)
        if not ok:
            # 不推进游标,下一轮重试;记忆服务不可用时由断路保护避免反复等待。
            continue
        with tenant_session(TenantSessionLocal) as db:
            room = db.get(Chatroom, plan.chatroom_id)
            if room is not None:
                commit_flush(room, plan, now=now)
                db.commit()
        flushed += 1
        logger.info("group memory digest flushed", extra={"chatroom_id": plan.chatroom_id, "messages": plan.count})
    return flushed
