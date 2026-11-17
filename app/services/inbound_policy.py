"""入站消息共用的原始时间与执行时效规则，存档流程不依赖执行是否允许。"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from app.core.time import SHANGHAI_TZ, ensure_shanghai_datetime, now_shanghai

MESSAGE_COMMAND_MAX_AGE = timedelta(minutes=10)


def parse_timestamp_value(value: Any) -> datetime | None:
    """解析当前渠道使用的秒、毫秒或 ISO 时间，缺省零值不能当成过期消息。"""

    if isinstance(value, bool) or value in (None, ''):
        return None
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    try:
        if isinstance(value, str) and any(marker in value for marker in ('T', '-', ':')):
            return ensure_shanghai_datetime(datetime.fromisoformat(value.replace('Z', '+00:00')))
        # 部分渠道携带小数秒；保留精度，避免截断后把尚未满十分钟的消息误判过期。
        numeric = float(value)
        if numeric <= 0:
            return None
        if numeric > 10_000_000_000:
            numeric /= 1000
        return datetime.fromtimestamp(numeric, tz=SHANGHAI_TZ)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def resolve_event_time(normalized: Mapping[str, Any]) -> datetime:
    """资料观测和免打扰共用原始时间；渠道未提供时间时沿用接收时刻。"""

    details = normalized.get('details')
    value = details.get('timestamp') if isinstance(details, Mapping) else None
    return parse_timestamp_value(value) or now_shanghai()


def expired_message_dispatch(normalized: Mapping[str, Any], *, now: datetime | None = None) -> dict | None:
    """超过十分钟仅返回存档摘要，供入队、消费和 AI 下发边界使用。

    时效必须以渠道原始发送时间计算，不能因入库或重试刷新时间。缺失有效时间
    时不推断过期；未来时间也不按旧消息丢弃。恰好十分钟仍在可执行范围内。
    """

    details = normalized.get('details')
    original = parse_timestamp_value(details.get('timestamp')) if isinstance(details, Mapping) else None
    if original is None:
        return None
    age = ensure_shanghai_datetime(now or now_shanghai()) - original
    if age <= MESSAGE_COMMAND_MAX_AGE:
        return None
    return {'mode': 'storage_only', 'status': 'skipped', 'queued': False, 'reason': 'stale_message',
        'event_time': original.isoformat(), 'event_time_display': original.strftime('%Y-%m-%d %H:%M:%S'),
        'age_seconds': int(age.total_seconds()), 'max_age_seconds': int(MESSAGE_COMMAND_MAX_AGE.total_seconds())}
