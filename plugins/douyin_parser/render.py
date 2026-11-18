"""抖音网页渲染解析：``__ac_signature`` 签名后读取作品页 ``RENDER_DATA``。

这是 DTK 之外的备用解析链路，参考 BEelzebub594/DouyinParser 的
``__ac_nonce`` / ``__ac_signature`` / ``RENDER_DATA`` 流程实现。
所有请求仍走平台 ``PluginHttpClient``，受响应体上限和总时限约束。
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any
from urllib.parse import unquote

from app.services.plugin_runtime.http_client import PluginHttpClient
from plugins.douyin_parser.parsers import (
    clean_text,
    dedupe_http_urls,
    image_candidates,
    normalize_duration_seconds,
)


DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SHARE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
)
NONCE_URL = "https://www.douyin.com/"
RENDER_PAGE_URL = "https://www.douyin.com/user/self?modal_id={item_id}&showTab=like"

_RENDER_DATA_RE = re.compile(r'<script id="RENDER_DATA" type="application/json">(.*?)</script>', re.S)
_ITEM_ID_RE = re.compile(r"/(?:video|note|slides|share)/(\d{10,})(?:/|$)")
_NONCE_RE = re.compile(r"__ac_nonce=([^;]+)")
_NONCE_PAGE_MAX_BYTES = 2 * 1024 * 1024
_PAGE_MAX_BYTES = 4 * 1024 * 1024
_SHARE_PAGE_MAX_BYTES = 3 * 1024 * 1024


class RenderError(ValueError):
    """网页渲染解析没有取得有效媒资。"""


def parse_render_media(
    client: PluginHttpClient,
    share_url: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    """按分享链接解析作品页，返回统一媒资结构。"""

    item_id = _resolve_item_id(client, share_url, deadline)
    nonce = _fetch_nonce(client, deadline)
    signature = _sign(nonce)
    response = client.request(
        "GET",
        RENDER_PAGE_URL.format(item_id=item_id),
        headers={
            "User-Agent": DESKTOP_USER_AGENT,
            "Referer": "https://www.douyin.com/",
            "Cookie": f"__ac_nonce={nonce}; __ac_signature={signature}",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        total_timeout_seconds=_remaining(deadline, 15),
        max_response_bytes=_PAGE_MAX_BYTES,
        max_redirects=1,
    )
    if response.status_code != 200:
        raise RenderError(f"作品页 HTTP {response.status_code}")
    detail = _extract_detail(response.text)
    media = media_from_render_detail(detail)
    # 卡片跳转用作品页而不是 aweme.snssdk.com 播放直链:后者的 favicon 是空图标,
    # 微信会把卡片底部的来源 logo 显示成空白。
    media["page_url"] = f"https://www.douyin.com/video/{item_id}"
    return media


def media_from_render_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """把 RENDER_DATA 里的 ``videoDetail`` 转成插件统一媒资结构。"""

    if not isinstance(detail, dict):
        raise RenderError("作品页没有有效数据")
    author = clean_text((detail.get("authorInfo") or {}).get("nickname"))
    title = clean_text(detail.get("desc") or detail.get("caption") or detail.get("itemTitle"))
    images = detail.get("images")
    if isinstance(images, list) and images:
        pages: list[dict[str, Any]] = []
        for image in images:
            if not isinstance(image, dict):
                continue
            candidates = image.get("urlList") or image.get("url_list") or []
            urls = image_candidates({"url_list": candidates})
            if urls:
                pages.append(
                    {
                        "media_type": "image",
                        "image_candidates": urls,
                        "video_candidates": [],
                        "cover_candidates": urls,
                    }
                )
        if pages:
            return {"type": "image", "title": title, "author": author, "note_pages": pages}

    video = detail.get("video")
    if not isinstance(video, dict):
        raise RenderError("作品没有视频或图片数据")
    uri = str(video.get("uri") or "").strip()
    play_addr = video.get("playAddr")
    fallback_url = ""
    if isinstance(play_addr, list):
        for item in play_addr:
            candidate = item.get("src") if isinstance(item, dict) else item
            if isinstance(candidate, str) and candidate.startswith("http"):
                fallback_url = candidate
                break
    elif isinstance(play_addr, str):
        fallback_url = play_addr
    url = (
        f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=720p&line=0"
        if uri
        else html.unescape(fallback_url).strip()
    )
    if not url:
        raise RenderError("作品没有可用的播放地址")
    cover = str(video.get("cover") or "").strip()
    if not cover.startswith("http"):
        cover_candidates = dedupe_http_urls(video.get("coverUrlList"))
        cover = cover_candidates[0] if cover_candidates else ""
    return {
        "type": "video",
        "url": html.unescape(url),
        "title": title,
        "author": author,
        "cover": html.unescape(cover),
        "duration_seconds": normalize_duration_seconds(video.get("duration"), milliseconds=True),
    }


def _resolve_item_id(client: PluginHttpClient, share_url: str, deadline: float) -> str:
    item_id = _item_id_from_url(share_url)
    if item_id:
        return item_id
    response = client.request(
        "GET",
        share_url,
        headers={
            "User-Agent": SHARE_USER_AGENT,
            "Referer": "https://www.douyin.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
        total_timeout_seconds=_remaining(deadline, 15),
        max_response_bytes=_SHARE_PAGE_MAX_BYTES,
        max_redirects=5,
    )
    item_id = _item_id_from_url(response.url or share_url)
    if item_id:
        return item_id
    raise RenderError("未找到抖音作品编号")


def _item_id_from_url(url: str) -> str:
    match = _ITEM_ID_RE.search(str(url or ""))
    return match.group(1) if match else ""


def _fetch_nonce(client: PluginHttpClient, deadline: float) -> str:
    response = client.request(
        "GET",
        NONCE_URL,
        headers={"User-Agent": DESKTOP_USER_AGENT, "Accept": "text/html"},
        total_timeout_seconds=_remaining(deadline, 10),
        max_response_bytes=_NONCE_PAGE_MAX_BYTES,
        max_redirects=1,
    )
    match = _NONCE_RE.search(str(response.headers.get("set-cookie") or ""))
    if not match:
        raise RenderError("未获取到抖音页面 nonce")
    return match.group(1).strip()


def _sign(nonce: str) -> str:
    try:
        # 延迟导入：只有选择网页渲染解析时才加载 V8 原生组件。
        from plugins.douyin_parser.render_sign import sign_ac

        return sign_ac(nonce, DESKTOP_USER_AGENT)
    except Exception as exc:  # noqa: BLE001 - 原生库缺失或签名失败都按解析失败处理
        raise RenderError("抖音页面签名组件不可用") from exc


def _extract_detail(page_html: str) -> dict[str, Any]:
    match = _RENDER_DATA_RE.search(page_html or "")
    if not match:
        raise RenderError("抖音作品页未包含 RENDER_DATA")
    try:
        payload = json.loads(unquote(match.group(1)))
        detail = payload.get("app", {}).get("videoDetail")
    except (ValueError, TypeError, AttributeError) as exc:
        raise RenderError("抖音作品页数据解析失败") from exc
    if not isinstance(detail, dict):
        raise RenderError("抖音作品页未返回作品数据")
    return detail


def _remaining(deadline: float, cap: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0.5:
        raise RenderError("整体时限已用尽")
    return min(cap, left)
