from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom


def mark_chatroom_discovered(
    db: Session,
    *,
    channel: Channel,
    chatroom: Chatroom,
    seen_at: datetime | None = None,
    registration_source: str = "passive",
    enable_management: bool = False,
) -> Chatroom:
    resolved_seen_at = _normalize_dt(seen_at)
    if not getattr(chatroom, "first_seen_at", None):
        chatroom.first_seen_at = resolved_seen_at
    chatroom.last_seen_at = resolved_seen_at
    chatroom.registration_source = str(getattr(chatroom, "registration_source", None) or registration_source or "passive")
    # 自动发现的新群默认进入纳管，确保群首次产生消息后即可参与插件调度；
    # 已存在群不会传入该标记，因此后台手动停用、归档或历史待启用状态都保持不变。
    current_management_status = str(getattr(chatroom, "management_status", "") or "").strip().lower()
    if enable_management and current_management_status not in {"disabled", "archived"}:
        chatroom.management_status = "enabled"
        chatroom.is_registered = True
        chatroom.is_active = True
    if not getattr(chatroom, "management_status", None):
        chatroom.management_status = "discovered"
    if chatroom.management_status == "pending_sync":
        chatroom.management_status = "discovered"
    if getattr(chatroom, "is_registered", None) is None:
        chatroom.is_registered = True
    if getattr(chatroom, "is_active", None) is None:
        chatroom.is_active = False
    if not chatroom.bot_id:
        chatroom.bot_id = channel.bot_id
    if not chatroom.channel_id:
        chatroom.channel_id = channel.id
    return chatroom


def upsert_manual_chatroom(
    db: Session,
    *,
    channel: Channel,
    external_id: str,
    name: str | None = None,
    remark: str | None = None,
    operator_note: str | None = None,
) -> Chatroom:
    normalized_external_id = str(external_id or "").strip()
    if not normalized_external_id:
        raise ValueError("external_id is required")
    chatroom = db.scalar(
        select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == normalized_external_id)
    )
    created = False
    if not chatroom:
        chatroom = Chatroom(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            external_id=normalized_external_id,
            status="pending_sync",
        )
        db.add(chatroom)
        db.flush()
        created = True
    mark_chatroom_discovered(db, channel=channel, chatroom=chatroom, registration_source="manual")
    chatroom.registration_source = "manual"
    chatroom.is_registered = True
    if created and not chatroom.management_status:
        chatroom.management_status = "discovered"
    if name is not None:
        chatroom.name = str(name or "").strip() or normalized_external_id
    elif not chatroom.name:
        chatroom.name = normalized_external_id
    if remark is not None:
        chatroom.remark = str(remark or "").strip() or None
    if operator_note is not None:
        chatroom.operator_note = str(operator_note or "").strip() or None
    return chatroom


def set_chatroom_management_status(chatroom: Chatroom, status: str) -> Chatroom:
    normalized = str(status or "").strip().lower()
    if normalized == "enabled":
        chatroom.management_status = "enabled"
        chatroom.is_registered = True
        chatroom.is_active = True
    elif normalized == "disabled":
        chatroom.management_status = "disabled"
        chatroom.is_registered = True
        chatroom.is_active = False
    elif normalized == "archived":
        chatroom.management_status = "archived"
        chatroom.is_registered = True
        chatroom.is_active = False
    else:
        chatroom.management_status = "discovered"
        chatroom.is_registered = True
        chatroom.is_active = False
    return chatroom


# 群的三档运行模式,复用管理状态字段:
#   enabled  正常运行:采集消息,并触发插件、群功能和 AI;
#   disabled 只采集消息:继续接收并记录消息,但不触发任何插件、群功能和 AI;
#   archived 完全关闭:不接收、不记录消息,也不触发任何功能。
GROUP_MODE_RUN = "enabled"
GROUP_MODE_COLLECT_ONLY = "disabled"
GROUP_MODE_OFF = "archived"

# 状态在页面上的中文说法(群管理列表、群详情共用)。
MANAGEMENT_STATUS_LABELS = {
    "enabled": "正常运行",
    "disabled": "只采集消息",
    "archived": "已关闭",
    "discovered": "待启用",
}

# 群详情页「运行模式」的三个选项:值、名称、说明。
GROUP_MODE_OPTIONS = (
    {"value": GROUP_MODE_RUN, "label": "正常运行", "hint": "采集消息，并响应插件、群功能和 AI 对话"},
    {"value": GROUP_MODE_COLLECT_ONLY, "label": "只采集消息", "hint": "继续接收并记录消息，但不触发任何插件、群功能和 AI 对话"},
    {"value": GROUP_MODE_OFF, "label": "完全关闭", "hint": "不接收、不记录消息，也不触发任何功能"},
)


def format_chatroom_management_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    return MANAGEMENT_STATUS_LABELS.get(normalized, normalized or "待启用")


def is_collection_off(chatroom: Chatroom | None) -> bool:
    """群是否处于“完全关闭”:入站消息直接丢弃,不采集。"""

    return chatroom is not None and str(getattr(chatroom, "management_status", "") or "").strip().lower() == GROUP_MODE_OFF


def management_tone(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "enabled":
        return "success"
    if normalized in {"disabled", "archived"}:
        return "muted"
    return "outline"


def _normalize_dt(value: datetime | None) -> datetime:
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    return now_shanghai()
