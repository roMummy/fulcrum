from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from urllib import request as urlrequest

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = "multi-bot-admin/1.0"
DEFAULT_SOURCES = {
    "nbc": "https://feeds.nbcnews.com/nbcnews/public/news",
    "cnn": "http://rss.cnn.com/rss/edition.rss",
    "abc": "https://abcnews.go.com/abcnews/topstories",
    "fox": "http://feeds.foxnews.com/foxnews/latest?format=xml",
    "bbc": "http://feeds.bbci.co.uk/news/world/rss.xml",
}


def _text_reply(content: str) -> list[dict[str, str]]:
    normalized = str(content).strip()
    return [{"type": "text", "content": normalized}] if normalized else []


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    sources = request.config.get("sources") or list(DEFAULT_SOURCES.keys())
    if not isinstance(sources, list):
        sources = list(DEFAULT_SOURCES.keys())
    per_source_limit = int(request.config.get("per_source_limit", 5) or 5)
    timeout = int(request.config.get("default_timeout", 10) or 10)
    dify_api_url = str(request.config.get("dify_api_url") or "").strip()
    authorization = str(request.config.get("authorization") or "").strip()

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="全球新闻插件已就绪，可执行新闻抓取和摘要。",
            output={
                "sources": sources,
                "per_source_limit": per_source_limit,
                "default_timeout": timeout,
                "dify_enabled": bool(dify_api_url and authorization),
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    items = _fetch_sources(sources=sources, per_source_limit=per_source_limit, timeout=timeout)
    summary = ""
    provider_status = "fetched"
    if items and dify_api_url and authorization:
        try:
            summary = _summarize_with_dify(dify_api_url=dify_api_url, authorization=authorization, items=items)
            provider_status = "summarized"
        except Exception as exc:
            summary = f"Dify 摘要失败: {exc}"
            provider_status = "fetched_without_summary"

    reply_text = summary or _build_headline_digest(items)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"全球新闻抓取完成，共 {len(items)} 条新闻。",
        output={
            "provider_status": provider_status,
            "items": items,
            "count": len(items),
            "summary": summary,
            "sources": sources,
            "reply_text": reply_text,
            "replies": _text_reply(reply_text),
        },
    )


def _fetch_sources(*, sources: list[str], per_source_limit: int, timeout: int) -> list[dict]:
    items: list[dict] = []
    for source in sources:
        source_key = str(source).strip().lower()
        feed_url = DEFAULT_SOURCES.get(source_key)
        if not feed_url:
            continue
        try:
            items.extend(_fetch_rss_items(source=source_key, url=feed_url, limit=per_source_limit, timeout=timeout))
        except Exception:
            continue
    return items


def _fetch_rss_items(*, source: str, url: str, limit: int, timeout: int) -> list[dict]:
    req = urlrequest.Request(url, method="GET", headers={"User-Agent": DEFAULT_USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    root = ET.fromstring(raw)
    items: list[dict] = []
    for node in root.findall(".//item")[:limit]:
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub_date = (node.findtext("pubDate") or "").strip()
        if not title:
            continue
        items.append({"source": source, "title": title, "link": link, "published_at": pub_date})
    return items


def _summarize_with_dify(*, dify_api_url: str, authorization: str, items: list[dict]) -> str:
    query = "\n".join(f"[{item['source']}] {item['title']} {item['link']}" for item in items)
    body = {
        "response_mode": "blocking",
        "conversation_id": "",
        "inputs": {},
        "query": query,
        "user": "multi-bot-admin-global-news",
    }
    response = _post_json(
        dify_api_url,
        body,
        headers={"Authorization": authorization, "Content-Type": "application/json; charset=utf-8"},
    )
    for key in ("answer", "text", "message"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    raise ValueError("missing summary text")


def _post_json(url: str, body: dict, *, headers: dict[str, str]) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")


def _build_headline_digest(items: list[dict]) -> str:
    lines: list[str] = []
    for item in items[:5]:
        source = str(item.get("source") or "").strip()
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        lines.append(f"[{source}] {title}" if source else title)
    return "\n".join(lines)
