"""抖音解析服务:取媒资(DTK 优先、自有解析兜底)并按媒资类型生成平台标准回复。

一、解析来源:DTK 自建服务优先;未配置、失败或无有效媒资时回退到自有解析:
   展开短链取分享页 -> 官方 detail 接口(a_bogus 签名)-> 分享页 HTML -> slidesinfo 接口。
   所有请求都走平台 PluginHttpClient,受响应体上限、重定向上限和总时限约束。
二、发送策略:
   - 图文: 先发作者与文案;实况页发视频加封面,普通页发图片;普通图超过 3 张时纵向合并分页。
   - 视频: download_mode=file 时发文件;默认只有时长明确且不超过 20 秒才下载直发,
     其余(长视频、时长未知、下载失败)统一发链接卡片。
"""

from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from PIL import Image

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import (
    MAX_REPLY_COUNT,
    PluginReplyError,
    file_reply,
    image_reply,
    link_reply,
    text_reply,
    video_reply,
)
from plugins.douyin_parser.a_bogus import generate_a_bogus
from plugins.douyin_parser.parsers import clean_text, map_dtk_data, parse_aweme, parse_share_html

# ================================================================ 解析来源

# a_bogus 会把 UA 摘要写入签名,detail 的签名与请求必须使用完全相同的字符串。
DETAIL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1"
)
DETAIL_API_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
TTWID_API_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_TTL_SECONDS = 3600.0
_ITEM_ID_PATTERN = re.compile(r"/(?:video|note|slides|share)/?(\d{10,})(?:/|$)")
_SLIDES_PATTERN = re.compile(r"/share/slides/(\d+)(?:/|$)")
_TTWID_PATTERN = re.compile(r"(?:^|[;,]\s*)ttwid=([^;]+)")
_PAGE_MAX_BYTES = 3 * 1024 * 1024
_JSON_MAX_BYTES = 4 * 1024 * 1024

# ttwid 是公共浏览器身份 Cookie,不属于用户凭据,按进程缓存一小时即可。
_ttwid_lock = threading.Lock()
_ttwid_cache: tuple[str, float] = ("", 0.0)


class SourceError(RuntimeError):
    """某个解析来源没有取得有效媒资;只携带不含密钥的简短原因。"""


def parse_media(
    client: PluginHttpClient,
    share_url: str,
    *,
    dtk_api_url: str,
    dtk_api_key: str,
    dtk_wait_seconds: int,
    deadline: float,
) -> dict[str, Any]:
    """按 DTK、自有解析的固定顺序取媒资;两条都失败才抛错,不使用任何第三方兜底。"""

    reasons: list[str] = []
    if dtk_api_url and dtk_api_key:
        try:
            return _from_dtk(client, share_url, dtk_api_url, dtk_api_key, dtk_wait_seconds, deadline)
        except (SourceError, PluginHttpError, ValueError) as exc:
            reasons.append(f"DTK:{exc}")
    try:
        return _from_local(client, share_url, deadline)
    except (SourceError, PluginHttpError, ValueError) as exc:
        reasons.append(f"自有解析:{exc}")
    raise SourceError("; ".join(reasons))


def _remaining(deadline: float, cap: float) -> float:
    """单次请求的总时限:不超过 cap,也不超过整体剩余预算。"""

    left = deadline - time.monotonic()
    if left <= 0.5:
        raise SourceError("整体时限已用尽")
    return min(cap, left)


# ---------------------------------------------------------------- DTK

def _from_dtk(
    client: PluginHttpClient,
    share_url: str,
    api_url: str,
    api_key: str,
    wait_seconds: int,
    deadline: float,
) -> dict[str, Any]:
    """DTK 是异步队列,wait 参数把它变成同步调用:200 完成,202 仍在跑(跳过),失败带 error.code。"""

    response = client.post_json(
        f"{api_url.rstrip('/')}/api/v1/parse?wait={int(wait_seconds)}",
        {"url": share_url, "include_raw": True},
        headers={"X-API-Key": api_key, "Accept": "application/json"},
        # urllib 的单次超时同时约束“等待响应字节”;DTK 的 wait 模式会挂起到任务完成,
        # 所以它必须不小于 wait,否则任务还在跑就被误判超时(此前固定 5 秒导致慢任务全部失败)。
        connect_timeout_seconds=wait_seconds + 5,
        total_timeout_seconds=_remaining(deadline, wait_seconds + 5),
        max_response_bytes=_JSON_MAX_BYTES,
        max_redirects=1,
    )
    if response.status_code == 202:
        raise SourceError("任务未在等待时间内完成")
    try:
        body = response.json()
    except ValueError as exc:
        raise SourceError(f"返回非 JSON(HTTP {response.status_code})") from exc
    if not isinstance(body, dict):
        raise SourceError("返回结构异常")
    if not body.get("success"):
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        raise SourceError(f"错误码 {error.get('code') or 'UNKNOWN'}")
    data = body.get("data")
    media = map_dtk_data(data) if isinstance(data, dict) else None
    if not media:
        raise SourceError("没有有效媒资")
    return media


# ---------------------------------------------------------------- 自有解析

def _from_local(client: PluginHttpClient, share_url: str, deadline: float) -> dict[str, Any]:
    """展开短链并取分享页,再依次尝试 detail、HTML、slidesinfo。"""

    page = client.request(
        "GET",
        share_url,
        headers={"User-Agent": MOBILE_USER_AGENT, "Referer": "https://www.douyin.com/", "Accept-Language": "zh-CN,zh;q=0.9"},
        total_timeout_seconds=_remaining(deadline, 15),
        max_response_bytes=_PAGE_MAX_BYTES,
        max_redirects=5,
    )
    resolved = page.url or share_url

    item_id = _item_id(resolved)
    if item_id:
        try:
            return _from_detail(client, item_id, deadline)
        except (SourceError, PluginHttpError, ValueError):
            pass  # 签名、ttwid 与目标站点状态都会变化,失败后继续走页面解析

    media = parse_share_html(page.text)
    if media:
        return media
    media = _from_slides(client, resolved, deadline)
    if media:
        return media
    raise SourceError("官方接口与分享页都没有可用作品")


def _item_id(resolved_url: str) -> str:
    match = _ITEM_ID_PATTERN.search(urlsplit(resolved_url).path or "")
    return match.group(1) if match else ""


def _detail_query(item_id: str) -> str:
    """按网页端字段顺序构造 detail 查询串;字段顺序会参与签名,不要调整。"""

    return urlencode(
        {
            "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web", "aweme_id": item_id,
            "update_version_code": "170400", "pc_client_type": "1", "version_code": "170400",
            "version_name": "17.4.0", "cookie_enabled": "true", "screen_width": "1536",
            "screen_height": "864", "browser_language": "zh-CN", "browser_platform": "Win32",
            "browser_name": "Chrome", "browser_version": "123.0.0.0", "browser_online": "true",
            "engine_name": "Blink", "engine_version": "123.0.0.0", "os_name": "Windows",
            "os_version": "10", "cpu_core_num": "16", "device_memory": "8", "platform": "PC",
            "downlink": "10", "effective_type": "4g", "round_trip_time": "50",
        }
    )


def _ttwid(client: PluginHttpClient, deadline: float) -> str:
    """取 detail 接口使用的 ttwid 身份 Cookie,命中缓存时不发请求。"""

    global _ttwid_cache
    with _ttwid_lock:
        value, stamp = _ttwid_cache
        if value and time.time() - stamp < TTWID_TTL_SECONDS:
            return value
    response = client.post_json(
        TTWID_API_URL,
        {
            "region": "cn", "aid": 1768, "needFid": False, "service": "www.ixigua.com",
            "migrate_info": {"ticket": "", "source": "node"}, "cbUrlProtocol": "https", "union": True,
        },
        headers={"User-Agent": DETAIL_USER_AGENT, "Accept": "application/json, text/plain, */*"},
        total_timeout_seconds=_remaining(deadline, 10),
        max_response_bytes=256 * 1024,
        max_redirects=0,
    )
    match = _TTWID_PATTERN.search(str(response.headers.get("set-cookie") or ""))
    if response.status_code != 200 or not match:
        raise SourceError("获取 ttwid 失败")
    value = match.group(1).strip()
    with _ttwid_lock:
        _ttwid_cache = (value, time.time())
    return value


def _from_detail(client: PluginHttpClient, item_id: str, deadline: float) -> dict[str, Any]:
    """用本地 a_bogus 签名调用官方 detail 接口。"""

    ttwid = _ttwid(client, deadline)
    query = _detail_query(item_id)
    # 签名字符表含 /、+、= 等字符,必须整体编码;query 已由 urlencode 生成,不能二次编码。
    url = f"{DETAIL_API_URL}?{query}&a_bogus={quote(generate_a_bogus(query, DETAIL_USER_AGENT), safe='')}"
    response = client.request(
        "GET",
        url,
        headers={
            "Cookie": f"ttwid={ttwid}", "Referer": "https://www.douyin.com/",
            "User-Agent": DETAIL_USER_AGENT, "Accept": "application/json, text/plain, */*",
        },
        total_timeout_seconds=_remaining(deadline, 15),
        max_response_bytes=_JSON_MAX_BYTES,
        max_redirects=0,
    )
    if response.status_code != 200:
        raise SourceError(f"detail 接口 HTTP {response.status_code}")
    payload = response.json()
    aweme = payload.get("aweme_detail") if isinstance(payload, dict) and payload.get("status_code") == 0 else None
    media = parse_aweme(aweme) if isinstance(aweme, dict) and aweme.get("aweme_id") else None
    if not media:
        raise SourceError("detail 接口没有作品数据")
    return media


def _from_slides(client: PluginHttpClient, resolved_url: str, deadline: float) -> dict[str, Any] | None:
    """新版图文页可能只剩页面壳,但地址保留 /share/slides/{id},此时调用同站点 slidesinfo 接口。"""

    parts = urlsplit(resolved_url)
    match = _SLIDES_PATTERN.search(parts.path or "")
    if not match or parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    query = urlencode({"aweme_ids": json.dumps([int(match.group(1))], separators=(",", ":")), "request_source": 200})
    response = client.request(
        "GET",
        f"{parts.scheme}://{parts.netloc}/web/api/v2/aweme/slidesinfo/?{query}",
        headers={"User-Agent": MOBILE_USER_AGENT, "Referer": resolved_url, "Accept": "application/json, text/plain, */*"},
        total_timeout_seconds=_remaining(deadline, 10),
        max_response_bytes=_JSON_MAX_BYTES,
        max_redirects=0,
    )
    payload = response.json()
    details = payload.get("aweme_details") if isinstance(payload, dict) and payload.get("status_code") == 0 else None
    if isinstance(details, list) and details and isinstance(details[0], dict):
        return parse_aweme(details[0])
    return None


# ================================================================ 发送策略

SHORT_VIDEO_MAX_SECONDS = 20.0  # 含 20 秒本身;用浮点比较,避免 20.001 秒被截整成短视频
MERGE_THRESHOLD = 3  # 普通图文超过这个张数才合并
MERGE_WIDTH = 1242
MERGE_MAX_HEIGHT = 30000  # 单张合并图高度上限,保证 JPEG 的 base64 不超过平台 16MB 限制
MAX_IMAGE_DOWNLOADS = 18
IMAGE_DOWNLOAD_BYTES = 12 * 1024 * 1024
COVER_DOWNLOAD_BYTES = 2 * 1024 * 1024
VIDEO_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 与微桥 send_video 默认 10MiB 上限保持一致
MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DOWNLOAD_WORKERS = 4


def build_replies(
    client: PluginHttpClient,
    media: dict[str, Any],
    *,
    download_mode: str,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    """入口:按媒资类型生成回复列表,总数不超过平台单次上限。"""

    if media.get("type") == "image":
        return _note_replies(client, media, timeout_seconds)
    return _video_replies(client, media, download_mode, timeout_seconds)


# ---------------------------------------------------------------- 图文

def _note_replies(client: PluginHttpClient, media: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    pages = list(media.get("note_pages") or [])[:MAX_IMAGE_DOWNLOADS]
    replies: list[dict[str, Any]] = []
    note_text = _note_text(media)
    if note_text:
        replies.append(text_reply(note_text))
    budget = MAX_REPLY_COUNT - len(replies)

    if any(page.get("media_type") == "video" for page in pages):
        # 实况图文:逐页决定发视频还是图片,每页一条回复。
        with ThreadPoolExecutor(_DOWNLOAD_WORKERS) as pool:
            page_replies = list(pool.map(lambda page: _live_page_reply(client, page, timeout), pages[:budget]))
        replies += [reply for reply in page_replies if reply]
    else:
        with ThreadPoolExecutor(_DOWNLOAD_WORKERS) as pool:
            images = [
                data
                for data in pool.map(
                    lambda page: _first_image(client, page.get("image_candidates"), timeout),
                    pages,
                )
                if data
            ]
        outputs = _merge_vertical(images)[:budget] if len(images) > MERGE_THRESHOLD else images[:budget]
        replies += [image_reply(base64_data=_b64(data)) for data in outputs]
    if not any(reply["type"] in {"image", "video"} for reply in replies):
        raise PluginReplyError("图文素材下载失败")
    return replies


def _note_text(media: dict[str, Any]) -> str:
    lines = []
    if media.get("author"):
        lines.append(f"作者:{media['author']}")
    if media.get("title"):
        lines.append(f"文案:{media['title']}")
    return "\n".join(lines)


def _live_page_reply(client: PluginHttpClient, page: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """实况页:视频和封面都拿到就发视频,否则降级成这一页的静态图。"""

    if page.get("media_type") == "video":
        video = _first_media(client, page.get("video_candidates"), VIDEO_DOWNLOAD_BYTES, timeout, video=True)
        cover = _first_image(client, page.get("cover_candidates"), timeout)
        if video and cover:
            return video_reply(video_base64=_b64(video), cover_base64=_b64(cover))
    image = _first_image(client, page.get("image_candidates"), timeout)
    return image_reply(base64_data=_b64(image)) if image else None


# ---------------------------------------------------------------- 视频

def _video_replies(client: PluginHttpClient, media: dict[str, Any], mode: str, timeout: float) -> list[dict[str, Any]]:
    url = str(media.get("url") or "").strip()
    if not url:
        raise PluginReplyError("没有可用的视频地址")
    title = clean_text(media.get("title")) or "抖音作品"
    author = clean_text(media.get("author"))
    if mode == "file":
        return [file_reply(file_name=_safe_name(f"{author} - {title}" if author else title) + ".mp4", file_url=url)]

    duration = media.get("duration_seconds")
    cover_url = str(media.get("cover") or "")
    if isinstance(duration, (int, float)) and duration <= SHORT_VIDEO_MAX_SECONDS:
        video = _download(client, url, VIDEO_DOWNLOAD_BYTES, timeout, video=True)
        cover = _first_image(client, [cover_url], timeout) if video and cover_url else None
        # 平台视频回复必须带封面;没有封面或视频下载失败就回退卡片。
        if video and cover:
            return [video_reply(video_base64=_b64(video), cover_base64=_b64(cover), play_length=round(duration))]
    return [
        link_reply(
            url=url,
            title=author or "抖音作品",
            description=title,
            thumb_url=cover_url if cover_url.startswith("http") else "",
        )
    ]


# ---------------------------------------------------------------- 下载与图片处理

def _download(client: PluginHttpClient, url: str, limit: int, timeout: float, *, video: bool = False) -> bytes | None:
    """下载单个资源;失败、非 200、类型不符都返回 None,由上层换候选或降级。"""

    try:
        response = client.request(
            "GET",
            url.strip(),
            headers={"User-Agent": MEDIA_USER_AGENT, "Referer": "https://www.douyin.com/"},
            total_timeout_seconds=timeout,
            max_response_bytes=limit,
            max_redirects=3,
        )
    except PluginHttpError:
        return None
    if response.status_code != 200 or not response.body:
        return None
    if video:
        kind = str(response.headers.get("content-type") or "").lower()
        if "video" not in kind and "octet-stream" not in kind:
            return None
    return response.body


def _first_media(client, candidates, limit: int, timeout: float, *, video: bool = False) -> bytes | None:  # noqa: ANN001
    """按候选顺序下载第一个可用资源,降低单个 CDN 地址失效导致整体失败的概率。"""

    for url in candidates or []:
        data = _download(client, str(url), limit, timeout, video=video)
        if data:
            return data
    return None


def _first_image(client, candidates, timeout: float) -> bytes | None:  # noqa: ANN001
    """下载第一张可用图片并统一转成 JPEG(微信对 WEBP 兼容性不稳)。"""

    data = _first_media(client, candidates, IMAGE_DOWNLOAD_BYTES, timeout)
    return _to_jpeg(data) if data else None


def _rgb(image: Image.Image) -> Image.Image:
    """带透明通道或调色板的图片铺白底转 RGB,避免保存 JPEG 时报错。"""

    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def _to_jpeg(data: bytes) -> bytes | None:
    """不是可识别图片(例如错误页)时返回 None,不把垃圾数据当图片发出去。"""

    try:
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            _rgb(image).save(output, format="JPEG", quality=92)
            return output.getvalue()
    except Exception:  # noqa: BLE001 - Pillow 对坏图会抛多种异常
        return None


def _merge_vertical(images: list[bytes]) -> list[bytes]:
    """把多张图缩放到统一宽度后纵向拼接,累计高度超限就另起一页。"""

    pages: list[list[Image.Image]] = [[]]
    height = 0
    for data in images:
        try:
            with Image.open(io.BytesIO(data)) as source:
                image = _rgb(source)
                if image.width != MERGE_WIDTH:
                    image = image.resize((MERGE_WIDTH, max(1, int(image.height * MERGE_WIDTH / image.width))))
                else:
                    image = image.copy()
        except Exception:  # noqa: BLE001
            continue
        if pages[-1] and height + image.height > MERGE_MAX_HEIGHT:
            pages.append([])
            height = 0
        pages[-1].append(image)
        height += image.height

    outputs: list[bytes] = []
    for page in pages:
        if not page:
            continue
        canvas = Image.new("RGB", (MERGE_WIDTH, sum(item.height for item in page)))
        offset = 0
        for item in page:
            canvas.paste(item, (0, offset))
            offset += item.height
        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=85)
        outputs.append(buffer.getvalue())
    return outputs


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", re.sub(r"\s+", " ", value)).strip(" ._")
    return (cleaned or "douyin")[:200]
