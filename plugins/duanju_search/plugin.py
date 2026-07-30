from __future__ import annotations

import json
import urllib.parse
from urllib import request as urlrequest

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    api_url = str(request.config.get("api_url") or "https://api.uuuka.com/api/search").strip()
    limit = int(request.config.get("result_limit", 3) or 3)
    timeout = int(request.config.get("default_timeout", 10) or 10)

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="短剧搜索插件已就绪，可继续执行搜索请求。",
            output={"api_url": api_url, "result_limit": limit, "default_timeout": timeout},
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    keyword = str(
        request.payload.get("keyword")
        or request.payload.get("command_args")
        or request.payload.get("stripped_query")
        or request.payload.get("stripped_content")
        or request.payload.get("query")
        or request.payload.get("content")
        or ""
    ).strip()
    if not keyword:
        return PluginExecutionResult(ok=False, status="error", message="短剧搜索执行缺少 keyword/query/content。")

    try:
        items = _search(api_url=api_url, keyword=keyword, limit=limit, timeout=timeout)
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"短剧搜索请求失败: {exc}")

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"短剧搜索完成，共返回 {len(items)} 条结果。",
        output={
            "keyword": keyword,
            "provider_status": "found" if items else "empty",
            "items": items,
            "result_count": len(items),
        },
    )


def _search(*, api_url: str, keyword: str, limit: int, timeout: int) -> list[dict]:
    query = urllib.parse.urlencode({"keyword": keyword, "page": 1, "limit": limit})
    target_url = f"{api_url}{'&' if '?' in api_url else '?'}{query}"
    data = _get_json(target_url, timeout)
    if not bool(data.get("success")):
        raise ValueError(str(data.get("message") or "provider returned unsuccessful response"))
    raw_items = data.get("data", {}).get("items", []) if isinstance(data.get("data"), dict) else []
    items: list[dict] = []
    for item in raw_items[:limit]:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "title": str(item.get("title") or "").strip(),
                "source_link": str(item.get("source_link") or "").strip(),
                "update_time": str(item.get("update_time") or "").strip(),
                "view_count": int(item.get("view_count") or 0),
            }
        )
    return items


def _get_json(url: str, timeout: int) -> dict:
    req = urlrequest.Request(url, method="GET", headers={"accept": "application/json"})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")
