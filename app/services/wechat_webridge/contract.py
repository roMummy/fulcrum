"""WeBridge 8080 长轮询协议的稳定字段契约。

本模块只保存协议层常量和轻量字段解析，业务落库由 service.py 负责，避免路由
直接依赖插件请求的原始字段。
"""

from __future__ import annotations

from typing import Any

AGENT_PATHS = ("register", "state", "event", "poll", "command_result")
JOB_TYPE = "wechat_webridge_outbound"


def as_object(value: Any) -> dict[str, Any]:
    """把未知输入收敛成对象，避免恶意数组或字符串进入业务分支。"""

    return value if isinstance(value, dict) else {}


def text(value: Any) -> str:
    """统一清理协议文本字段，并限制单字段长度。"""

    return str(value or "").strip()[:512]


def identity_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """提取设备、会话和微信账号三元身份；不保存原始状态大对象。"""

    return {
        "device_id": text(payload.get("device_id")),
        "session_id": text(payload.get("session_id")),
        "account_id": text(payload.get("account_id")),
        "runtime_version": text(payload.get("runtime_version")),
        "build_marker": text(payload.get("build_marker")),
    }
