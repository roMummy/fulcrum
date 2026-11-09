from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import standard_replies, text_reply


COMMANDS = ("短剧", "短剧搜索", "搜索短剧", "短剧查询")


@dataclass(frozen=True)
class DuanjuConfig:
    """保存租户自备短剧搜索接口配置。"""

    api_url: str
    api_key: str
    result_limit: int
    timeout_seconds: int


@dataclass(frozen=True)
class DramaResult:
    """保存可以直接展示给用户的最小短剧结果。"""

    title: str
    source_link: str
    update_time: str
    view_count: int


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """调用租户短剧接口并返回紧凑文本，不持久化上游结果数组。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="短剧搜索插件已就绪。",
            output={
                "api_url_configured": bool(config.api_url),
                "api_key_configured": bool(config.api_key),
                "result_limit": config.result_limit,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not config.api_url:
        return _error_result("请先配置当前租户的短剧搜索服务地址。", provider_status="search_not_configured")

    keyword = _resolve_keyword(request.payload)
    if not keyword:
        return _error_result("请使用“短剧 剧名”搜索。", provider_status="keyword_required")
    try:
        results = _search(config, keyword=keyword)
    except (PluginHttpError, ValueError):
        return _error_result("短剧搜索服务暂时不可用，请稍后重试。", provider_status="search_failed")
    if not results:
        message = f"没有找到短剧：{keyword}"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=message,
            output={
                "provider_status": "empty",
                "keyword": keyword,
                "result_count": 0,
                "replies": standard_replies([text_reply(message)]),
            },
        )

    reply_text = _build_result_text(keyword, results)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"短剧搜索完成，共返回 {len(results)} 条结果。",
        output={
            "provider_status": "completed",
            "keyword": keyword,
            "result_count": len(results),
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def _search(config: DuanjuConfig, *, keyword: str) -> tuple[DramaResult, ...]:
    headers = {"Accept": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    separator = "&" if "?" in config.api_url else "?"
    url = (
        f"{config.api_url}{separator}"
        f"{urlencode({'keyword': keyword, 'page': 1, 'limit': config.result_limit})}"
    )
    response = PluginHttpClient().request(
        "GET",
        url,
        headers=headers,
        connect_timeout_seconds=min(8, config.timeout_seconds),
        total_timeout_seconds=config.timeout_seconds,
        max_response_bytes=2 * 1024 * 1024,
        max_redirects=2,
        retry_count=1,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"duanju search http {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("duanju search response invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("success") is not True:
        raise ValueError("duanju search rejected request")
    data = payload.get("data")
    raw_items = data.get("items") if isinstance(data, Mapping) else None
    if not isinstance(raw_items, list):
        return ()

    results: list[DramaResult] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, Mapping):
            continue
        title = _clean_title(raw_item.get("title"))
        if not title:
            continue
        results.append(
            DramaResult(
                title=title,
                source_link=_public_http_url(raw_item.get("source_link")),
                update_time=_inline_text(raw_item.get("update_time")),
                view_count=_bounded_nonnegative_int(raw_item.get("view_count"), maximum=1_000_000_000),
            )
        )
        if len(results) >= config.result_limit:
            break
    return tuple(results)


def _build_result_text(keyword: str, results: tuple[DramaResult, ...]) -> str:
    lines = [f"短剧搜索：{keyword}"]
    for index, item in enumerate(results, start=1):
        lines.append("")
        lines.append(f"{index}. {item.title}")
        if item.source_link:
            lines.append(f"链接：{item.source_link}")
        if item.update_time:
            lines.append(f"更新：{item.update_time}")
        if item.view_count > 0:
            lines.append(f"观看：{item.view_count}")
    return "\n".join(lines)


def _resolve_keyword(payload: Mapping[str, Any]) -> str:
    explicit = str(payload.get("keyword") or payload.get("command_args") or "").strip()
    if explicit:
        return _inline_text(explicit)
    content = _inline_text(payload.get("content") or payload.get("message_text") or payload.get("query"))
    if content.startswith(("/", "#")):
        content = content[1:].lstrip()
    for command in COMMANDS:
        if content == command:
            return ""
        if content.startswith(f"{command} "):
            return content[len(command) :].strip()
    return content


def _clean_title(value: Any) -> str:
    normalized = _inline_text(value)
    normalized = normalized.removeprefix("短剧《").removesuffix("》免费观看")
    return normalized.strip(" 《》")


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


def _load_config(raw_config: Mapping[str, Any]) -> DuanjuConfig:
    raw = dict(raw_config)
    return DuanjuConfig(
        api_url=str(raw.get("api_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        result_limit=_bounded_int(raw.get("result_limit"), default=3, minimum=1, maximum=5),
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=15, minimum=5, maximum=60),
    )


def _bounded_nonnegative_int(value: Any, *, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(normalized, maximum))


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
