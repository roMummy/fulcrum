"""开放 Webhook 事件目录和统一信封字段。"""

from __future__ import annotations


OPEN_EVENT_TYPES = (
    "message.received",
    "message.sent",
    "message.failed",
    "file.received",
    "account.online",
    "account.offline",
    "account.reconnected",
    "job.completed",
    "job.failed",
)


def is_open_event_type(value: str) -> bool:
    """判断事件类型是否属于对外稳定目录。"""

    return str(value or "").strip() in OPEN_EVENT_TYPES


def build_open_event_id(*, event_type: str, scope_id: int | str, sequence: int | str) -> str:
    """生成可重试复用的确定性事件 ID，外部系统可按此字段幂等消费。"""

    return f"evt_{str(event_type).strip()}_{int(scope_id)}_{int(sequence)}"

