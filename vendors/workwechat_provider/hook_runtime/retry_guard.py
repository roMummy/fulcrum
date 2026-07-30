# -*- coding: utf8 -*-
from __future__ import annotations


DEFAULT_INJECTION_RETRY_COOLDOWN_SECONDS = 30.0


def parse_injection_retry_cooldown(value: object, default: float = DEFAULT_INJECTION_RETRY_COOLDOWN_SECONDS) -> float:
    """解析注入失败后的重试冷却时间。

    环境变量来自用户机器或打包配置，可能为空、非数字或过大。这里统一限制到
    5~300 秒，避免健康检查把 Hook 注入打成高频循环，也避免配置错误导致长期不重试。
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = float(default)
    return max(5.0, min(seconds, 300.0))


def injection_retry_delay_remaining(last_failed_at: float, now: float, cooldown_seconds: float) -> float:
    """计算距离下一次允许注入还剩多少秒。"""
    if not last_failed_at:
        return 0.0
    elapsed = max(0.0, float(now) - float(last_failed_at))
    return max(0.0, float(cooldown_seconds) - elapsed)
