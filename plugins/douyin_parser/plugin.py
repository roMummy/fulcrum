"""抖音解析插件入口:识别消息里的抖音分享链接,解析后按媒资类型生成回复。

解析来源与发送策略见 sources.py 和 delivery.py;这里只负责匹配、配置和结果封装。
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.core.logging import get_logger
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import PluginReplyError, standard_replies, text_reply

from plugins.douyin_parser.service import SourceError, build_replies, parse_media

logger = get_logger(__name__)


# 只匹配 URL 合法的 ASCII 字符,紧跟在链接后的中文文案和中文标点会被自然截断。
DOUYIN_URL_PATTERN = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+", flags=re.IGNORECASE)
ALLOWED_SHARE_HOSTS = ("douyin.com", "iesdouyin.com")
DOWNLOAD_MODES = {"card", "file"}


@dataclass(frozen=True)
class DouyinConfig:
    """抖音解析配置:DTK 服务地址、密钥和发送模式。"""

    dtk_api_url: str
    dtk_api_key: str
    dtk_wait_seconds: int
    download_mode: str
    timeout_seconds: int


def matches_event(context) -> dict[str, object]:
    """只匹配文本或应用消息中的公开抖音分享地址。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    share_url = _extract_douyin_url(str(getattr(context, "content", "") or ""))
    matched = chat_type in {"direct", "group"} and message_type in {"text", "app"} and bool(share_url)
    return {
        "matched": matched,
        "reason": "douyin_share_url_hit" if matched else "douyin_share_url_required",
        "debug": {
            "chat_type": chat_type,
            "message_type": message_type,
            "share_host": urlsplit(share_url).hostname if share_url else "",
        },
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """解析分享链接并生成回复;整体受 timeout_seconds 约束,失败只给用户通用提示。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="抖音解析插件已就绪。",
            output={
                "dtk_configured": bool(config.dtk_api_url and config.dtk_api_key),
                "download_mode": config.download_mode,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    share_url = _extract_douyin_url(_resolve_content(request.payload))
    if not share_url:
        return _error("消息中没有可识别的抖音分享链接。", "share_url_required")

    client = PluginHttpClient()
    deadline = time.monotonic() + config.timeout_seconds
    try:
        media = parse_media(
            client,
            share_url,
            dtk_api_url=config.dtk_api_url,
            dtk_api_key=config.dtk_api_key,
            dtk_wait_seconds=config.dtk_wait_seconds,
            deadline=deadline,
        )
    except (SourceError, PluginHttpError, ValueError) as exc:
        # 失败原因只含来源与错误码,不含密钥;写入日志便于区分 key 无效、超时和站点风控。
        logger.warning("抖音解析失败", extra={"reason": str(exc)[:300], "share_url": share_url})
        return _error("抖音解析服务暂时不可用,请稍后重试。", "parse_failed")
    try:
        replies = build_replies(
            client,
            media,
            download_mode=config.download_mode,
            timeout_seconds=min(30, max(5, int(deadline - time.monotonic()) + 10)),
        )
    except (PluginReplyError, PluginHttpError):
        return _error("素材下载失败,请稍后重试。", "media_failed")

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已解析抖音{'图文' if media.get('type') == 'image' else '视频'}。",
        output={
            "provider_status": "completed",
            "media_type": media.get("type"),
            "title": media.get("title") or "",
            "author": media.get("author") or "",
            "replies": standard_replies(replies),
        },
    )


def _extract_douyin_url(content: str) -> str:
    """提取第一条合法抖音链接:裁掉中文标点,并拒绝伪造域名和内嵌账号密码。"""

    for match in DOUYIN_URL_PATTERN.finditer(html.unescape(str(content or ""))):
        candidate = match.group(0).rstrip(".,;:!?)]")
        parsed = urlsplit(candidate)
        host = str(parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
            and any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_SHARE_HOSTS)
        ):
            return candidate
    return ""


def _resolve_content(payload: Mapping[str, Any]) -> str:
    return str(payload.get("url") or payload.get("content") or payload.get("message_text") or payload.get("query") or "")


def _error(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status, "replies": standard_replies([text_reply(message)])},
    )


def _load_config(raw_config: Mapping[str, Any]) -> DouyinConfig:
    raw = dict(raw_config)
    mode = str(raw.get("download_mode") or "card").strip().lower()
    return DouyinConfig(
        dtk_api_url=str(raw.get("dtk_api_url") or "").strip().rstrip("/"),
        dtk_api_key=str(raw.get("dtk_api_key") or "").strip(),
        dtk_wait_seconds=_bounded_int(raw.get("dtk_wait_seconds"), default=15, minimum=3, maximum=30),
        download_mode=mode if mode in DOWNLOAD_MODES else "card",
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=25, minimum=5, maximum=28),
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
