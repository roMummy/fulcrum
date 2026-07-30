from __future__ import annotations

import json
from html import escape
import urllib.parse
from urllib import request as urlrequest

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


VIDEO_XML_TEMPLATE = """
<appmsg appid="" sdkver="1">
    <title>{title}-click to watch</title>
    <des>{des}</des>
    <type>5</type>
    <url>{url}</url>
    <thumburl>{thumburl}</thumburl>
</appmsg>
<scene>0</scene>
<appinfo>
    <version>1</version>
    <appname></appname>
</appinfo>
<commenturl></commenturl>
""".strip()


def _text_reply(content: str) -> list[dict[str, str]]:
    normalized = str(content).strip()
    return [{"type": "text", "content": normalized}] if normalized else []


def _xml_reply(xml: str) -> list[dict[str, str]]:
    normalized = str(xml).strip()
    return [{"type": "xml", "xml": normalized}] if normalized else []


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    timeout = int(request.config.get("default_timeout", 10) or 10)
    download_mode = str(request.config.get("download_mode") or "card").strip() or "card"
    internal_api_url = str(request.config.get("internal_api_url") or "http://192.168.2.32:8999/api/hybrid/video_data").strip()
    external_api_url = str(request.config.get("external_api_url") or "https://api.pearktrue.cn/api/video/api.php").strip()
    external_api_key = str(request.config.get("external_api_key") or "f56c1fed0c6e64e7").strip()

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="douyin parser is ready",
            output={
                "download_mode": download_mode,
                "internal_api_url": internal_api_url,
                "external_api_url": external_api_url,
                "default_timeout": timeout,
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")

    content = str(
        request.payload.get("url")
        or request.payload.get("command_args")
        or request.payload.get("stripped_content")
        or request.payload.get("stripped_query")
        or request.payload.get("content")
        or request.payload.get("query")
        or ""
    ).strip()
    if not content:
        return PluginExecutionResult(ok=False, status="error", message="missing url/content/query")
    url = _extract_douyin_url(content)
    if not url:
        return PluginExecutionResult(ok=False, status="error", message="no valid douyin share url found")

    try:
        media = _parse_media(
            url=url,
            internal_api_url=internal_api_url,
            external_api_url=external_api_url,
            external_api_key=external_api_key,
            timeout=timeout,
        )
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"douyin parser failed: {exc}")

    output = {"provider_status": "parsed", "download_mode": download_mode, "url": url, **media}
    replies: list[dict[str, str]] = []
    if output.get("type") == "video" and output.get("url"):
        output["video_xml"] = _build_video_xml(output)
        replies.extend(_xml_reply(str(output.get("video_xml") or "")))
    elif output.get("type") == "image":
        output["reply_text"] = _build_image_reply_text(output)
        replies.extend(_text_reply(str(output.get("reply_text") or "")))
    output["replies"] = replies
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"douyin parser completed: {output.get('type') or 'video'}",
        output=output,
    )


def _extract_douyin_url(content: str) -> str:
    marker = "https://v.douyin.com/"
    start = content.find(marker)
    if start < 0:
        return ""
    return content[start:].split()[0].strip().rstrip(";")


def _parse_media(*, url: str, internal_api_url: str, external_api_url: str, external_api_key: str, timeout: int) -> dict:
    primary = _parse_from_internal_api(internal_api_url=internal_api_url, url=url, timeout=timeout)
    if primary and (primary.get("url") or primary.get("images")):
        return primary
    secondary = _parse_from_external_api(
        external_api_url=external_api_url,
        external_api_key=external_api_key,
        url=url,
        timeout=timeout,
    )
    if secondary and (secondary.get("url") or secondary.get("images")):
        return secondary
    raise ValueError("no valid media data")


def _parse_from_internal_api(*, internal_api_url: str, url: str, timeout: int) -> dict | None:
    query = urllib.parse.urlencode({"url": url, "minimal": "false"})
    body = _get_json(f"{internal_api_url}?{query}", timeout)
    if int(body.get("code") or 0) != 200:
        return None
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return None
    author = data.get("author") or {}
    author_name = str(
        (author.get("nickname") if isinstance(author, dict) else "")
        or (author.get("unique_id") if isinstance(author, dict) else "")
        or "unknown"
    ).strip()
    if data.get("aweme_type") == 68 or data.get("images") or data.get("image_list"):
        images: list[str] = []
        for image in data.get("images") or []:
            if not isinstance(image, dict):
                continue
            selected = _prefer_image_url(image.get("url_list") or image.get("download_url_list") or [])
            if selected:
                images.append(selected)
        if images:
            return {
                "type": "image",
                "images": images,
                "title": str(data.get("desc") or data.get("caption") or "").strip(),
                "author": author_name,
                "cover": images[0],
            }
        return None
    video = data.get("video") or {}
    play_url = ""
    if isinstance(video, dict):
        for bit_rate in sorted(
            [item for item in (video.get("bit_rate") or []) if isinstance(item, dict)],
            key=lambda item: int(item.get("bit_rate") or 0),
            reverse=True,
        ):
            play_addr = bit_rate.get("play_addr") or {}
            selected = _prefer_video_url(play_addr.get("url_list") or [])
            if selected:
                play_url = selected
                break
        if not play_url:
            play_url = _prefer_video_url((video.get("play_addr") or {}).get("url_list") or []) or ""
    cover_url = ""
    if isinstance(video, dict):
        cover = video.get("cover") or {}
        if isinstance(cover, dict):
            cover_url = str((cover.get("url_list") or [""])[0] or "").strip()
    if not play_url:
        return None
    return {
        "type": "video",
        "url": play_url,
        "title": str(data.get("caption") or data.get("desc") or "untitled").strip(),
        "author": author_name,
        "cover": cover_url,
    }


def _parse_from_external_api(*, external_api_url: str, external_api_key: str, url: str, timeout: int) -> dict | None:
    query = urllib.parse.urlencode({"url": url, "key": external_api_key})
    body = _post_json(f"{external_api_url}?{query}", timeout)
    if int(body.get("code") or 0) != 200:
        return None
    data = body.get("data") or {}
    if not isinstance(data, dict):
        return None
    return {
        "type": "video",
        "url": str(data.get("url") or "").strip(),
        "title": str(data.get("title") or "").strip(),
        "author": str(data.get("author") or "unknown").strip(),
        "cover": str(data.get("cover") or "").strip(),
    }


def _build_video_xml(media: dict) -> str:
    return VIDEO_XML_TEMPLATE.format(
        title=escape(str(media.get("author") or "unknown")),
        des=escape(str(media.get("title") or "untitled")),
        url=escape(str(media.get("url") or "")),
        thumburl=escape(str(media.get("cover") or "")),
    )


def _build_image_reply_text(media: dict) -> str:
    title = str(media.get("title") or "").strip()
    author = str(media.get("author") or "").strip()
    images = [str(item).strip() for item in (media.get("images") or []) if str(item).strip()]
    lines: list[str] = []
    if author:
        lines.append(f"作者: {author}")
    if title:
        lines.append(f"标题: {title}")
    if images:
        lines.append("图集:")
        lines.extend(images[:5])
    return "\n".join(lines)


def _prefer_video_url(urls: list[str]) -> str | None:
    cleaned = [str(item).strip().strip("`") for item in urls if str(item).strip()]
    if not cleaned:
        return None
    for item in cleaned:
        netloc = urllib.parse.urlparse(item).netloc.lower()
        if "douyinvod.com" in netloc and netloc.startswith(("v3", "v4", "v5", "v6", "v7", "v8", "v9", "v10", "v11")):
            return item
    for item in cleaned:
        if "douyinvod.com" in urllib.parse.urlparse(item).netloc.lower():
            return item
    return cleaned[0]


def _prefer_image_url(urls: list[str]) -> str | None:
    cleaned = [str(item).strip().strip("`") for item in urls if str(item).strip()]
    if not cleaned:
        return None
    for item in cleaned:
        lower = item.lower()
        if ".jpeg" in lower or lower.endswith(".jpg"):
            return item
    for item in cleaned:
        if ".webp" in item.lower():
            return item
    return cleaned[0]


def _get_json(url: str, timeout: int) -> dict:
    req = urlrequest.Request(url, method="GET", headers={"accept": "application/json"})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")


def _post_json(url: str, timeout: int) -> dict:
    req = urlrequest.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("accept", "application/json")
    with urlrequest.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")
