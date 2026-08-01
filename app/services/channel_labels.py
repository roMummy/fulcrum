from __future__ import annotations


CHANNEL_TYPE_LABELS = {
    "wechat_ipad": "微信 iPad 协议",
    "wechat_winauto": "微信 Windows",
    "workwechat": "企业微信 Hook",
    "telegram": "Telegram",
    "simulator": "模拟器",
    "openclaw": "OpenClaw",
}

# SaaS 首期产品边界只开放企业微信。旧渠道标签和适配器继续保留，供历史代码、
# 独立回归测试及后续迁移审计使用，但任何新的租户配置都必须经过本允许列表。
SAAS_ENABLED_CHANNEL_TYPES = ("workwechat",)


def require_saas_enabled_channel_type(value: str | None) -> str:
    """规范化并校验 SaaS 可写渠道类型，供页面、服务和开放接口共同复用。"""

    normalized = str(value or "").strip()
    if normalized not in SAAS_ENABLED_CHANNEL_TYPES:
        raise ValueError("channel_type_not_enabled")
    return normalized


def get_channel_type_label(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return "-"
    return CHANNEL_TYPE_LABELS.get(normalized, normalized)
