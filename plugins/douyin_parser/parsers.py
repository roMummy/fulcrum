"""抖音作品数据归一化:把 DTK 响应、官方 detail、分享页 HTML 统一转成同一种媒资结构。

本模块只做纯数据转换,不发起任何网络请求,便于单元测试。统一结构:
- 视频: {"type": "video", "url", "title", "author", "cover", "duration_seconds"}
- 图文: {"type": "image", "title", "author", "note_pages": [页描述...]}
  每页 {"media_type": "image"|"video", "image_candidates", "video_candidates", "cover_candidates"}
"""

from __future__ import annotations

import html
import json
import math
import re
from typing import Any
from urllib.parse import urlparse

ROUTER_DATA_PATTERN = re.compile(r"window\._ROUTER_DATA\s*=\s*({.*?})\s*</script>", re.S)
_LEGACY_PLAY_PATTERN = re.compile(r'"play_addr":\s*{\s*"uri":\s*"[^"]*",\s*"url_list":\s*\[([^\]]*)\]')
_COVER_KEYS = ("cover", "origin_cover", "dynamic_cover", "animated_cover", "ai_dynamic_cover")
# 官方 aweme 播放入口:uri 是稳定的资源标识,url_list 里常带临时域名或水印参数。
_AWEME_PLAY_URL = "https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=720p&line=0"


# ---------------------------------------------------------------- 文本清理

def clean_text(value: Any) -> str:
    """清理标题、作者等文本:反转义 HTML/unicode,并只在高置信度时修复乱码。"""

    if value is None:
        return ""
    text = html.unescape(str(value))
    if "\\u" in text or "\\x" in text:
        try:
            text = text.encode("utf-8").decode("unicode_escape")
        except Exception:  # noqa: BLE001 - 解码失败就保留原文
            pass
    return _repair_mojibake(text).strip()


def _repair_mojibake(text: str) -> str:
    """UTF-8 被按 Latin-1/CP1252 误解码的典型乱码,中文比例提升时才采用修复结果。"""

    if sum(text.count(mark) for mark in "ÃÂæäåçéêïð") < 2:
        return text
    for encoding in ("latin1", "cp1252"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except Exception:  # noqa: BLE001
            continue
        if _cjk_count(repaired) >= _cjk_count(text):
            return repaired
    return text


def _cjk_count(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def normalize_duration_seconds(value: Any, *, milliseconds: bool = False) -> float | None:
    """把单位明确的时长换算成正的有限秒数;无法确认时返回 None,不按数值大小猜单位。"""

    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    seconds = duration / 1000.0 if milliseconds else duration
    return seconds if seconds > 0 else None


# ---------------------------------------------------------------- 地址挑选

def dedupe_http_urls(urls: Any) -> list[str]:
    """反转义、去重并只保留 http(s) 地址,保持原顺序。"""

    result: list[str] = []
    for url in urls or []:
        if not isinstance(url, str):
            continue
        decoded = html.unescape(url).strip().strip("`")
        if decoded.startswith("http") and decoded not in result:
            result.append(decoded)
    return result


def pick_video_url(urls: Any) -> str:
    """优先把 playwm 换成无水印的 play,并优先 aweme.snssdk.com 直链。"""

    decoded = [html.unescape(str(u)).strip().replace("playwm", "play") for u in urls or [] if isinstance(u, str) and u.strip()]
    for url in decoded:
        if "aweme.snssdk.com" in url:
            return url
    return _prefer_cdn_host(decoded)


def _prefer_cdn_host(urls: list[str]) -> str:
    """按 v3~v11 的 douyinvod 域名、v3~v11 域名、douyinvod 域名、首个地址的顺序选择。"""

    cleaned = [u.strip().strip("`") for u in urls if u]
    hosts = [(u, urlparse(u).netloc) for u in cleaned]
    is_vx = lambda host: bool(re.match(r"^v(3|4|5|6|7|8|9|10|11)(?:[\-.]|$)", host, re.I))  # noqa: E731
    is_vod = lambda host: "douyinvod.com" in host.lower()  # noqa: E731
    for tests in (lambda h: is_vx(h) and is_vod(h), is_vx, is_vod):
        for url, host in hosts:
            if tests(host):
                return url
    return cleaned[0] if cleaned else ""


def _image_priority(url: str) -> tuple[int, str]:
    """微信兼容性:JPEG 优先于 PNG,PNG 优先于 WEBP。"""

    lowered = url.lower()
    for rank, marks in enumerate(((".jpeg", ".jpg"), (".png",), (".webp",))):
        if any(mark in lowered for mark in marks):
            return rank, lowered
    return 3, lowered


def _url_list(node: Any) -> list[Any]:
    return list(node.get("url_list") or []) if isinstance(node, dict) else []


def image_candidates(image_info: dict[str, Any]) -> list[str]:
    """提取单页图片候选:字段间保持主图优先,只在同一字段内把 JPEG/PNG 提前到 WEBP 前。"""

    groups = [
        list(image_info.get("url_list") or []),
        list(image_info.get("download_url_list") or []),
        _url_list(image_info.get("display_image")),
        _url_list(image_info.get("owner_watermark_image")),
        _url_list(image_info.get("thumbnail")),
        _url_list(image_info.get("origin_cover")),
    ]
    ordered: list[str] = []
    for group in groups:
        for url in sorted(dedupe_http_urls(group), key=_image_priority):
            if url not in ordered:
                ordered.append(url)
    return ordered


def live_video_candidates(image_info: dict[str, Any]) -> list[str]:
    """从实况图的 image.video 里提取可发送的视频地址,高码率优先。"""

    video = image_info.get("video")
    if not isinstance(video, dict):
        return []
    rows = sorted(
        (row for row in video.get("bit_rate") or [] if isinstance(row, dict)),
        key=lambda row: row.get("bit_rate") or 0,
        reverse=True,
    )
    groups = [_url_list(row.get("play_addr")) for row in rows]
    groups += [
        _url_list(video.get(key))
        for key in ("download_addr", "play_addr_h264", "play_addr", "play_addr_lowbr", "download_suffix_logo_addr")
    ]
    picked = [pick_video_url(group) for group in groups]
    return dedupe_http_urls(picked)


def pick_cover_url(video: dict[str, Any]) -> str:
    """多个封面字段里优先 JPEG/PNG(微信卡片更稳),都没有时退回第一个 http 地址。"""

    fallback = ""
    for key in _COVER_KEYS:
        cover = video.get(key)
        if not isinstance(cover, dict):
            continue
        for urls in (cover.get("url_list") or [], [cover.get("uri"), cover.get("url")]):
            candidates = dedupe_http_urls(urls)
            for url in candidates:
                path = url.split("?", 1)[0].lower()
                if path.endswith((".jpg", ".jpeg", ".png")) or "format=jpeg" in url.lower():
                    return url
            fallback = fallback or (candidates[0] if candidates else "")
    return fallback


# ---------------------------------------------------------------- 作品转换

def build_note_pages(image_infos: Any) -> list[dict[str, Any]]:
    """把图文作品整理成逐页发送描述;实况页额外带视频候选。"""

    pages: list[dict[str, Any]] = []
    for info in image_infos or []:
        if not isinstance(info, dict):
            continue
        images = image_candidates(info)
        if not images:
            continue
        videos = live_video_candidates(info)
        cover_urls = _url_list((info.get("video") or {}).get("cover")) if isinstance(info.get("video"), dict) else []
        pages.append(
            {
                "media_type": "video" if videos else "image",
                "image_candidates": images,
                "video_candidates": videos,
                "cover_candidates": dedupe_http_urls(cover_urls + images),
            }
        )
    return pages


def parse_note_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """解析图文作品;没有任何可用图片时返回 None。"""

    pages = build_note_pages(item.get("images") or item.get("image_infos"))
    if not pages:
        return None
    return {
        "type": "image",
        "title": clean_text(item.get("desc")),
        "author": clean_text((item.get("author") or {}).get("nickname")),
        "note_pages": pages,
    }


def parse_video_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """解析视频作品;原始 aweme 的 video.duration 单位是毫秒。"""

    video = item.get("video")
    if not isinstance(video, dict) or video.get("duration") == 0:
        return None
    url = pick_video_url(_url_list(video.get("play_addr")))
    if not url:
        return None
    return {
        "type": "video",
        "url": url,
        "title": clean_text(item.get("desc")),
        "author": clean_text((item.get("author") or {}).get("nickname")),
        "cover": pick_cover_url(video),
        "duration_seconds": normalize_duration_seconds(video.get("duration"), milliseconds=True),
    }


def parse_aweme(aweme: dict[str, Any], *, fallback_duration: float | None = None) -> dict[str, Any] | None:
    """解析一条原始 aweme:先图文后视频;视频播放地址用稳定 uri 重建。"""

    note = parse_note_item(aweme)
    if note:
        return note
    video = parse_video_item(aweme)
    if not video:
        return None
    if video.get("duration_seconds") is None:
        video["duration_seconds"] = fallback_duration
    play_addr = (aweme.get("video") or {}).get("play_addr")
    uri = str(play_addr.get("uri") or "") if isinstance(play_addr, dict) else ""
    if uri:
        video["url"] = _AWEME_PLAY_URL.format(uri=uri)
    return video


# ---------------------------------------------------------------- DTK 响应

def map_dtk_data(data: dict[str, Any]) -> dict[str, Any] | None:
    """把 DTK 的 data 映射成统一结构;优先复用 raw 原始 aweme,缺失时按归一化字段手工映射。"""

    duration = normalize_duration_seconds(data.get("duration_ms"), milliseconds=True)
    video_obj = data.get("video") if isinstance(data.get("video"), dict) else None
    if duration is None and video_obj:
        duration = normalize_duration_seconds(video_obj.get("duration_ms"), milliseconds=True)

    raw = data.get("raw")
    if isinstance(raw, dict):
        aweme = raw.get("aweme_detail") if "aweme_detail" in raw else raw
        if isinstance(aweme, dict) and aweme.get("aweme_id"):
            parsed = parse_aweme(aweme, fallback_duration=duration)
            if parsed:
                return parsed

    author = data.get("author")
    author_name = (author.get("nickname") or author.get("unique_id") or "") if isinstance(author, dict) else str(author or "")
    title = str(data.get("caption") or data.get("desc") or data.get("title") or "").strip()
    kind = str(data.get("content_kind") or data.get("type") or "").lower()

    images = data.get("images")
    if (kind == "image_album" or images) and isinstance(images, list):
        pages = []
        for image in images:
            if isinstance(image, dict):
                urls = image.get("urls") or image.get("url_list") or ([image["url"]] if image.get("url") else [])
            else:
                urls = [image]
            candidates = dedupe_http_urls(urls)
            if candidates:
                pages.append({"media_type": "image", "image_candidates": candidates, "video_candidates": [], "cover_candidates": candidates})
        if pages:
            return {"type": "image", "title": clean_text(title), "author": clean_text(author_name), "note_pages": pages}

    url = cover = ""
    if video_obj:
        url = str(video_obj.get("play_url") or video_obj.get("download_url") or video_obj.get("url") or "").strip()
        cover = str(video_obj.get("cover_url") or video_obj.get("cover") or "").strip()
    url = url or str(data.get("play_url") or data.get("video_url") or data.get("download_url") or data.get("url") or "").strip()
    if not cover:
        cover_field = data.get("cover")
        if isinstance(cover_field, dict):
            cover = str(cover_field.get("url") or (cover_field.get("url_list") or [""])[0] or "").strip()
        elif isinstance(cover_field, str):
            cover = cover_field.strip()
        cover = cover or str(data.get("cover_url") or "").strip()
    if not url:
        return None
    return {
        "type": "video",
        "url": url,
        "title": clean_text(title),
        "author": clean_text(author_name),
        "cover": cover,
        "duration_seconds": duration,
    }


# ---------------------------------------------------------------- 分享页 HTML

def extract_router_aweme(page_html: str) -> dict[str, Any] | None:
    """从分享页的 window._ROUTER_DATA 取第一条作品(图文与视频都在这里)。"""

    match = ROUTER_DATA_PATTERN.search(page_html or "")
    if not match:
        return None
    try:
        loader = json.loads(match.group(1)).get("loaderData")
    except (ValueError, AttributeError):
        return None
    if not isinstance(loader, dict):
        return None
    for page in loader.values():
        if not isinstance(page, dict):
            continue
        # 新版 note 页的 videoInfoRes 直接挂在当前节点,旧版多包一层。
        for holder in (page, *[n for n in page.values() if isinstance(n, dict)]):
            info = holder.get("videoInfoRes")
            items = info.get("item_list") if isinstance(info, dict) else None
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return items[0]
    return None


def parse_legacy_video(page_html: str) -> dict[str, Any] | None:
    """兼容没有 _ROUTER_DATA 的旧分享页:直接用正则取播放地址、文案、作者、封面。"""

    match = _LEGACY_PLAY_PATTERN.search(page_html or "")
    if not match:
        return None
    url = pick_video_url([part.strip().strip('"') for part in match.group(1).split(",")])
    if not url:
        return None

    def field(key: str) -> str:
        found = re.search(rf'"{re.escape(key)}":\s*"([^"]*)"', page_html)
        return clean_text(found.group(1)) if found else ""

    cover = re.search(r'"cover":\s*{\s*"url_list":\s*\[\s*"([^"]+)"', page_html)
    return {
        "type": "video",
        "url": url,
        "title": field("desc"),
        "author": field("nickname"),
        "cover": html.unescape(cover.group(1)) if cover else "",
        "duration_seconds": None,
    }


def parse_share_html(page_html: str) -> dict[str, Any] | None:
    """分享页解析:先 _ROUTER_DATA,再旧版正则。"""

    aweme = extract_router_aweme(page_html)
    if aweme:
        parsed = parse_aweme(aweme)
        if parsed:
            return parsed
    return parse_legacy_video(page_html)
