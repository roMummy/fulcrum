from __future__ import annotations


CHANNEL_TYPE_LABELS = {
    "wechat_ipad": "微信 iPad 协议",
    "wechat_winauto": "微信 Windows",
    "workwechat": "企业微信 Hook",
    "telegram": "Telegram",
    "simulator": "模拟器",
    "openclaw": "OpenClaw",
}


def get_channel_type_label(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "-"
    return CHANNEL_TYPE_LABELS.get(normalized, normalized)
