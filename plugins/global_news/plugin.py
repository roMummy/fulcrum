from __future__ import annotations

import base64
import re
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.core.markdown_to_image import convert_md_str_to_image_sync
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply
from app.services.plugin_runtime.text_generation import (
    PluginTextGenerationClient,
    PluginTextGenerationError,
)


COMMANDS = ("全球新闻", "国际新闻", "环球新闻", "政经新闻", "政治经济新闻")
DEFAULT_SOURCE_URLS = {
    "nbc": "https://feeds.nbcnews.com/nbcnews/public/news",
    "cnn": "http://rss.cnn.com/rss/edition.rss",
    "abc": "https://abcnews.go.com/abcnews/topstories",
    "fox": "http://feeds.foxnews.com/foxnews/latest?format=xml",
    "bbc": "http://feeds.bbci.co.uk/news/world/rss.xml",
}
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; MultiBotSaaS-News/2.0)"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
NEWS_IMAGE_ROOT = PROJECT_ROOT / "data" / "temp" / "md2image"


@dataclass(frozen=True)
class GlobalNewsConfig:
    """保存新闻抓取和租户自备文本生成服务配置。"""

    sources: tuple[str, ...]
    per_source_limit: int
    fetch_timeout_seconds: int
    llm_api_base: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    render_as_image: bool


@dataclass(frozen=True)
class NewsItem:
    """跨 RSS/Atom 来源统一使用的最小新闻条目。"""

    source: str
    title: str
    link: str
    published_at: str


@dataclass(frozen=True)
class NewsFetchResult:
    """保存一次并行抓取的条目和来源健康结果。"""

    items: tuple[NewsItem, ...]
    successful_sources: tuple[str, ...]
    failed_sources: tuple[str, ...]


def matches_event(context) -> dict[str, object]:
    """只匹配直聊或群聊文本中的全球新闻命令。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "").strip()
    command = _resolve_command(content)
    matched = chat_type in {"direct", "group"} and message_type == "text" and command in COMMANDS
    return {
        "matched": matched,
        "reason": "global_news_command_hit" if matched else "global_news_command_required",
        "debug": {"command_name": command, "chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """并行抓取全球新闻，并通过通用文本生成接口生成中文政经摘要。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="全球新闻插件已就绪。",
            output={
                "sources": list(config.sources),
                "per_source_limit": config.per_source_limit,
                "llm_configured": bool(config.llm_api_base and config.llm_model),
                "render_as_image": config.render_as_image,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not config.llm_api_base or not config.llm_model:
        return _error_result(
            "请先配置当前租户自己的模型 API 地址和模型名称。",
            provider_status="llm_not_configured",
        )

    fetch_result = _fetch_sources(config)
    if not fetch_result.items:
        return _error_result("全球新闻来源暂时无法访问，请稍后重试。", provider_status="fetch_failed")

    prompt = _build_analysis_prompt(fetch_result.items)
    context = request.context
    user_id = (
        f"tenant-{context.tenant_id}:binding-{context.binding_id}"
        if context
        else "global-news"
    )
    try:
        generated = PluginTextGenerationClient(
            api_base=config.llm_api_base,
            model=config.llm_model,
            api_key=config.llm_api_key,
        ).generate(
            system_prompt=(
                "你是一名全球政治经济新闻编辑。只根据输入标题与链接，用中文提炼重要事件、"
                "潜在影响和需要继续关注的风险；区分事实与判断，不编造未提供的信息。"
            ),
            user_prompt=prompt,
            user_id=user_id,
            timeout_seconds=config.llm_timeout_seconds,
            temperature=0.2,
            max_tokens=2200,
        )
    except PluginTextGenerationError:
        return _error_result("新闻分析模型暂时不可用，请检查租户模型配置。", provider_status="llm_failed")

    summary = _normalize_summary(generated.text)
    replies, reply_type = _build_news_replies(config, summary)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已分析 {len(fetch_result.items)} 条全球新闻。",
        output={
            "provider_status": "completed",
            "headline_count": len(fetch_result.items),
            "successful_source_count": len(fetch_result.successful_sources),
            "failed_source_count": len(fetch_result.failed_sources),
            "successful_sources": list(fetch_result.successful_sources),
            "failed_sources": list(fetch_result.failed_sources),
            "summary": summary,
            "token_usage": generated.token_usage,
            "reply_type": reply_type,
            "replies": replies,
        },
    )


def _fetch_sources(config: GlobalNewsConfig) -> NewsFetchResult:
    """并行抓取固定安全源，单个来源失败不会阻断其他来源。"""

    successful: list[str] = []
    failed: list[str] = []
    items_by_source: dict[str, tuple[NewsItem, ...]] = {}
    worker_count = max(1, min(len(config.sources), len(DEFAULT_SOURCE_URLS)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="global-news") as executor:
        futures = {
            executor.submit(
                _fetch_source,
                source,
                DEFAULT_SOURCE_URLS[source],
                limit=config.per_source_limit,
                timeout_seconds=config.fetch_timeout_seconds,
            ): source
            for source in config.sources
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                source_items = future.result()
            except (PluginHttpError, ET.ParseError, ValueError):
                failed.append(source)
                continue
            if source_items:
                successful.append(source)
                items_by_source[source] = source_items
            else:
                failed.append(source)

    # 并行完成顺序不稳定，最终仍按租户配置中的来源顺序合并，确保提示词和测试可重复。
    merged = tuple(item for source in config.sources for item in items_by_source.get(source, ()))
    return NewsFetchResult(
        items=merged,
        successful_sources=tuple(source for source in config.sources if source in successful),
        failed_sources=tuple(source for source in config.sources if source in failed),
    )


def _fetch_source(
    source: str,
    url: str,
    *,
    limit: int,
    timeout_seconds: int,
) -> tuple[NewsItem, ...]:
    """通过受控 HTTP 读取一个固定源，并使用 XML 解析器处理 RSS/Atom。"""

    response = PluginHttpClient().request(
        "GET",
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT},
        connect_timeout_seconds=min(8, timeout_seconds),
        total_timeout_seconds=timeout_seconds,
        max_response_bytes=1024 * 1024,
        max_redirects=3,
        retry_count=1,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"新闻源返回 HTTP {response.status_code}")
    return _parse_feed_items(source, response, limit=limit)


def _parse_feed_items(source: str, response: PluginHttpResponse, *, limit: int) -> tuple[NewsItem, ...]:
    """按元素本地名解析 RSS item 或 Atom entry，不使用字符串切割 XML。"""

    root = ET.fromstring(response.body)
    entries = [node for node in root.iter() if _local_name(node.tag) in {"item", "entry"}]
    items: list[NewsItem] = []
    for entry in entries:
        title = _find_child_text(entry, "title")
        if not title:
            continue
        link = _find_entry_link(entry)
        published_at = (
            _find_child_text(entry, "pubDate")
            or _find_child_text(entry, "published")
            or _find_child_text(entry, "updated")
        )
        items.append(
            NewsItem(
                source=source,
                title=_normalize_inline_text(title),
                link=_normalize_public_link(link),
                published_at=_normalize_inline_text(published_at),
            )
        )
        if len(items) >= limit:
            break
    return tuple(items)


def _build_analysis_prompt(items: tuple[NewsItem, ...]) -> str:
    """把公开新闻标题压缩为模型输入，不包含网页全文或抓取响应。"""

    lines = [
        f"[{index}] 来源={item.source} 标题={item.title} 时间={item.published_at or '未知'} 链接={item.link or '无'}"
        for index, item in enumerate(items, start=1)
    ]
    return (
        "请基于下面的全球新闻标题生成中文简报：\n"
        "1. 先列出 5-8 条最重要事件；\n"
        "2. 再概括政治、经济、科技与安全层面的共同趋势；\n"
        "3. 对无法从标题确认的内容明确写“仍需核实”；\n"
        "4. 保留对应编号，便于读者回看来源。\n\n"
        + "\n".join(lines)
    )


def _build_news_replies(config: GlobalNewsConfig, summary: str) -> tuple[list[dict[str, Any]], str]:
    """优先生成通用图片，渲染失败时返回同一份文本简报。"""

    if config.render_as_image:
        image_path: Path | None = None
        try:
            output_name = f"global-news-{uuid.uuid4().hex}.png"
            image_path = Path(
                convert_md_str_to_image_sync(
                    f"# 全球新闻简报\n\n{summary}",
                    output_name,
                    base_dir=PROJECT_ROOT,
                )
            ).resolve(strict=True)
            image_path.relative_to(NEWS_IMAGE_ROOT.resolve())
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            return standard_replies([image_reply(base64_data=encoded)]), "image"
        except Exception:
            pass
        finally:
            if image_path is not None:
                try:
                    image_path.relative_to(NEWS_IMAGE_ROOT.resolve())
                    image_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
    return standard_replies([text_reply(summary)]), "text"


def _error_result(message: str, *, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status, "replies": standard_replies([text_reply(message)])},
    )


def _load_config(raw_config: Mapping[str, Any]) -> GlobalNewsConfig:
    raw = dict(raw_config)
    raw_sources = raw.get("sources")
    sources: list[str] = []
    if isinstance(raw_sources, (list, tuple)):
        for item in raw_sources:
            source = str(item or "").strip().lower()
            if source in DEFAULT_SOURCE_URLS and source not in sources:
                sources.append(source)
    if not sources:
        sources = list(DEFAULT_SOURCE_URLS)
    return GlobalNewsConfig(
        sources=tuple(sources),
        per_source_limit=_bounded_int(raw.get("per_source_limit"), default=5, minimum=1, maximum=10),
        fetch_timeout_seconds=_bounded_int(
            raw.get("fetch_timeout_seconds"),
            default=15,
            minimum=5,
            maximum=60,
        ),
        llm_api_base=str(raw.get("llm_api_base") or "").strip().rstrip("/"),
        llm_api_key=str(raw.get("llm_api_key") or "").strip(),
        llm_model=str(raw.get("llm_model") or "").strip(),
        llm_timeout_seconds=_bounded_int(
            raw.get("llm_timeout_seconds"),
            default=60,
            minimum=10,
            maximum=180,
        ),
        render_as_image=_as_bool(raw.get("render_as_image"), default=True),
    )


def _resolve_command(content: str) -> str:
    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    return normalized.split(maxsplit=1)[0].strip() if normalized else ""


def _find_child_text(node: ET.Element, local_name: str) -> str:
    for child in node.iter():
        if child is not node and _local_name(child.tag) == local_name:
            return str(child.text or "").strip()
    return ""


def _find_entry_link(node: ET.Element) -> str:
    for child in node.iter():
        if child is node or _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if href:
            return href
        text = str(child.text or "").strip()
        if text:
            return text
    return ""


def _local_name(tag: Any) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _normalize_public_link(value: str) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return normalized


def _normalize_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_summary(value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > 18_000:
        return normalized[:18_000].rstrip() + "\n\n内容较长，已截断。"
    return normalized


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


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))
