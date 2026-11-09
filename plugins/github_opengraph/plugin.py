from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply


# 中文聊天中链接经常紧跟逗号或句号，提取阶段必须把这些句读符当作边界；
# 否则下一条 URL 会被并入首个 GitHub 路径，后续 URL 标准化已经无法恢复原链接。
GITHUB_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?github\.com/[^\s<>\"'\u3000，。！？；：、【】《》]+",
    re.IGNORECASE,
)
TRAILING_PUNCTUATION = ".,!?;:，。！？；：'\"`)]}>】》"
ALLOWED_GITHUB_HOSTS = {"github.com", "www.github.com"}
MAX_GITHUB_PATH_LENGTH = 1024
MAX_PREVIEW_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class GithubOpenGraphConfig:
    """保存当前租户绑定唯一生效的链接预览配置。"""

    match_mode: str
    max_links_per_message: int
    hash_salt: str
    request_timeout_seconds: int


def matches_event(context) -> dict[str, object]:
    """根据当前绑定匹配模式判断文本中是否包含有效 GitHub 仓库链接。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "")
    config = _load_config(getattr(context, "binding_config", {}) or {})
    links = _extract_github_links(content, config.match_mode, maximum=1)
    matched = chat_type in {"direct", "group"} and message_type == "text" and bool(links)
    return {
        "matched": matched,
        "reason": "github_url_hit" if matched else "github_url_required",
        "debug": {
            "chat_type": chat_type,
            "message_type": message_type,
            "match_mode": config.match_mode,
        },
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """下载 OpenGraph 图片到内存并返回平台标准图片回复。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="GitHub 链接预览插件运行正常。",
            output={
                "match_mode": config.match_mode,
                "max_links_per_message": config.max_links_per_message,
                "hash_salt_configured": bool(config.hash_salt),
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    content = _resolve_content(request.payload)
    links = _extract_github_links(content, config.match_mode, maximum=config.max_links_per_message)
    if not links:
        return _error_result("消息中没有可预览的 GitHub 链接。", "link_required")

    client = PluginHttpClient()
    replies: list[dict[str, str]] = []
    failed_count = 0
    for link in links:
        preview_url = _build_opengraph_url(link, config.hash_salt)
        try:
            image_base64 = _download_preview_image(client, preview_url, config.request_timeout_seconds)
        except (PluginHttpError, ValueError):
            failed_count += 1
            continue
        replies.append(image_reply(base64_data=image_base64))

    if not replies:
        return _error_result("GitHub 预览图片暂时不可用，请稍后重试。", "preview_failed")
    normalized_replies = standard_replies(replies, max_count=config.max_links_per_message)
    status = "partial" if failed_count else "completed"
    message = f"已生成 {len(normalized_replies)} 张 GitHub 预览图。"
    if failed_count:
        message += f" {failed_count} 个链接生成失败。"
    return PluginExecutionResult(
        ok=True,
        status=status,
        message=message,
        output={
            "provider_status": status,
            "matched_link_count": len(links),
            "preview_count": len(normalized_replies),
            "failed_count": failed_count,
            "replies": normalized_replies,
        },
    )


def _extract_github_links(content: str, match_mode: str, *, maximum: int) -> tuple[str, ...]:
    """按出现顺序提取、标准化和去重 GitHub 链接。"""

    normalized_content = str(content or "").strip()
    if not normalized_content:
        return ()
    if match_mode == "exact":
        exact = _normalize_github_url(normalized_content)
        return (exact,) if exact else ()

    result: list[str] = []
    bounded_maximum = max(1, min(int(maximum), 5))
    for match in GITHUB_URL_PATTERN.finditer(normalized_content):
        link = _normalize_github_url(match.group(0))
        if not link or link in result:
            continue
        result.append(link)
        if len(result) >= bounded_maximum:
            break
    return tuple(result)


def _normalize_github_url(raw_url: Any) -> str:
    """只接受 github.com 下至少包含 owner/repository 的无凭证地址。"""

    cleaned = str(raw_url or "").strip().rstrip(TRAILING_PUNCTUATION)
    if (
        not cleaned.lower().startswith(("http://", "https://"))
        or any(character.isspace() for character in cleaned)
        or len(cleaned) > 4096
    ):
        return ""
    parsed = urlsplit(cleaned)
    hostname = str(parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname not in ALLOWED_GITHUB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    path = str(parsed.path or "")
    if not path or len(path) > MAX_GITHUB_PATH_LENGTH:
        return ""
    raw_parts = [part for part in path.split("/") if part]
    if len(raw_parts) < 2:
        return ""

    # 每个路径段单独解码再编码，保留 GitHub 支持的安全字符，同时阻止编码斜杠
    # 改变 owner/repository 的层级结构。
    path_parts = [quote(unquote(part), safe="@-._~") for part in raw_parts]
    if not path_parts[0] or not path_parts[1]:
        return ""
    return f"https://github.com/{'/'.join(path_parts)}"


def _build_opengraph_url(github_url: str, hash_salt: str) -> str:
    """使用租户密文盐值生成稳定 OpenGraph 地址。"""

    parsed = urlsplit(github_url)
    path = str(parsed.path or "")
    hash_material = f"{hash_salt}|{github_url}" if hash_salt else github_url
    digest = hashlib.sha256(hash_material.encode("utf-8")).hexdigest()[:20]
    return f"https://opengraph.githubassets.com/{digest}{path}"


def _download_preview_image(
    client: PluginHttpClient,
    preview_url: str,
    timeout_seconds: int,
) -> str:
    """受限下载 OpenGraph 图片，不把图片或仓库内容写入磁盘。"""

    response = client.request(
        "GET",
        preview_url,
        headers={"Accept": "image/*"},
        connect_timeout_seconds=min(8, timeout_seconds),
        total_timeout_seconds=timeout_seconds,
        max_response_bytes=MAX_PREVIEW_RESPONSE_BYTES,
        max_redirects=2,
        retry_count=1,
    )
    content_type = str(response.headers.get("content-type") or "").lower()
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"OpenGraph 返回 HTTP {response.status_code}")
    if not content_type.startswith("image/") or not response.body:
        raise ValueError("OpenGraph 返回内容不是图片")
    return base64.b64encode(response.body).decode("ascii")


def _resolve_content(payload: Mapping[str, Any]) -> str:
    """只读取标准消息文本，不接受载荷提供预览地址、散列或图片内容。"""

    return str(payload.get("content") or payload.get("message_text") or "")


def _load_config(raw_config: Mapping[str, Any]) -> GithubOpenGraphConfig:
    """读取唯一配置字段并限制模式、数量和网络超时。"""

    raw = dict(raw_config)
    match_mode = str(raw.get("match_mode") or "fuzzy").strip().lower()
    if match_mode not in {"fuzzy", "exact"}:
        match_mode = "fuzzy"
    return GithubOpenGraphConfig(
        match_mode=match_mode,
        max_links_per_message=_bounded_int(raw.get("max_links_per_message"), default=3, minimum=1, maximum=5),
        hash_salt=str(raw.get("hash_salt") or ""),
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=15, minimum=3, maximum=60),
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """返回面向用户的最小错误文本，不暴露下载地址或内部异常。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
