from __future__ import annotations

from app.services.channel_catalog import get_channel_type_definition


def get_channel_type_label(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "-"
    try:
        return get_channel_type_definition(normalized).label
    except ValueError:
        # 历史审计页可能读取已不再支持的类型，显示原始编码比伪造渠道名称更准确。
        return normalized
