from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import file_reply, standard_replies, text_reply


DOUYIN_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)
ALLOWED_SHARE_HOSTS = ("douyin.com", "iesdouyin.com")


@dataclass(frozen=True)
class DouyinConfig:
    """保存租户自备抖音解析服务配置。"""

    enabled: bool
    api_url: str
    api_key: str
    timeout_seconds: int
    max_media_items: int


@dataclass(frozen=True)
class ParsedMedia:
    """统一描述解析接口返回的视频或图集。"""

    media_type: str
    title: str
    author: str
    urls: tuple[str, ...]


def matches_event(context) -> dict[str, object]:
    """只匹配文本或应用消息中的公开抖音分享地址。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "")
    share_url = _extract_douyin_url(content)
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
    """调用单一租户解析接口，并把媒体结果转换为平台标准文件回复。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="抖音解析插件已就绪。",
            output={
                "enabled": config.enabled,
                "api_url_configured": bool(config.api_url),
                "api_key_configured": bool(config.api_key),
                "max_media_items": config.max_media_items,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not config.enabled:
        return _error_result("抖音解析插件未启用。", provider_status="plugin_disabled")
    if not config.api_url:
        return _error_result("请先配置当前租户的抖音解析服务地址。", provider_status="parser_not_configured")

    content = _resolve_content(request.payload)
    share_url = _extract_douyin_url(content)
    if not share_url:
        return _error_result("消息中没有可识别的抖音分享链接。", provider_status="share_url_required")
    try:
        media = _parse_media(config, share_url=share_url)
    except (PluginHttpError, ValueError):
        return _error_result("抖音解析服务暂时不可用，请稍后重试。", provider_status="parse_failed")
    if media is None or not media.urls:
        return _error_result("没有解析到可用的视频或图集。", provider_status="media_empty")

    replies = _build_media_replies(media)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已解析抖音{('视频' if media.media_type == 'video' else '图集')}。",
        output={
            "provider_status": "completed",
            "media_type": media.media_type,
            "title": media.title,
            "author": media.author,
            "media_count": len(media.urls),
            "replies": replies,
        },
    )


def _parse_media(config: DouyinConfig, *, share_url: str) -> ParsedMedia | None:
    headers: dict[str, str] = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    response = PluginHttpClient().post_json(
        config.api_url,
        {"url": share_url},
        headers=headers,
        connect_timeout_seconds=min(8, config.timeout_seconds),
        total_timeout_seconds=config.timeout_seconds,
        max_response_bytes=2 * 1024 * 1024,
        max_redirects=2,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"douyin parser http {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("douyin parser response invalid") from exc
    if not isinstance(payload, Mapping) or int(payload.get("code") or 0) != 200:
        raise ValueError("douyin parser rejected request")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None

    media_type = str(data.get("type") or "").strip().lower()
    if media_type == "video":
        urls = _normalize_media_urls([data.get("url")], maximum=1)
    elif media_type in {"image", "images", "album"}:
        raw_images = data.get("images")
        urls = _normalize_media_urls(
            raw_images if isinstance(raw_images, list) else [],
            maximum=config.max_media_items,
        )
        media_type = "image"
    else:
        return None
    if not urls:
        return None
    return ParsedMedia(
        media_type=media_type,
        title=_inline_text(data.get("title") or data.get("desc") or "抖音作品"),
        author=_inline_text(data.get("author") or ""),
        urls=urls,
    )


def _build_media_replies(media: ParsedMedia) -> list[dict[str, Any]]:
    display_title = media.title or "抖音作品"
    header = f"{media.author} - {display_title}" if media.author else display_title
    replies: list[dict[str, Any]] = [text_reply(f"已解析：{header}")]
    if media.media_type == "video":
        replies.append(file_reply(file_name=_safe_file_name(header, suffix="mp4"), file_url=media.urls[0]))
    else:
        for index, media_url in enumerate(media.urls, start=1):
            replies.append(
                file_reply(
                    file_name=_safe_file_name(f"{header}-{index}", suffix="jpg"),
                    file_url=media_url,
                )
            )
    return standard_replies(replies)


def _extract_douyin_url(content: str) -> str:
    decoded = html.unescape(str(content or ""))
    for match in DOUYIN_URL_PATTERN.finditer(decoded):
        candidate = match.group(0).rstrip(".,;:!?，。；：！？)]}>")
        parsed = urlsplit(candidate)
        hostname = str(parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
            and any(hostname == host or hostname.endswith(f".{host}") for host in ALLOWED_SHARE_HOSTS)
        ):
            return candidate
    return ""


def _normalize_media_urls(values: list[Any], *, maximum: int) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        media_url = _public_http_url(value)
        if media_url and media_url not in normalized:
            normalized.append(media_url)
        if len(normalized) >= maximum:
            break
    return tuple(normalized)


def _public_http_url(value: Any) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return normalized


def _safe_file_name(value: str, *, suffix: str) -> str:
    normalized = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", _inline_text(value)).strip(" ._")
    return f"{(normalized or 'douyin')[:240]}.{suffix}"


def _resolve_content(payload: Mapping[str, Any]) -> str:
    return str(
        payload.get("url")
        or payload.get("content")
        or payload.get("message_text")
        or payload.get("query")
        or ""
    )


def _inline_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _error_result(message: str, *, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _load_config(raw_config: Mapping[str, Any]) -> DouyinConfig:
    raw = dict(raw_config)
    return DouyinConfig(
        enabled=_as_bool(raw.get("enabled"), default=True),
        api_url=str(raw.get("api_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=20, minimum=5, maximum=60),
        max_media_items=_bounded_int(raw.get("max_media_items"), default=5, minimum=1, maximum=7),
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
