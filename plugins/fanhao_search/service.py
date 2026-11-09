from __future__ import annotations

import html as html_lib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode, urljoin, urlsplit

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, validate_http_url
from plugins._shared.media import IMAGE_TYPES, MediaBoundaryError, validate_media_bytes


DEFAULT_COMMANDS = ("番号", "番号查询")
DEFAULT_IMAGE_COMMANDS = ("查番", "以图查番")
DEFAULT_JAVBUS_URL = "https://www.javbus.com"
DEFAULT_AVSCAN_URL = "https://avscan.cc"
# 内置备用站:201 直连主站常握手超时,主站失败后自动切换,无需用户配置。
DEFAULT_JAVBUS_FALLBACK_URLS = ("https://www.cdnbus.casa",)
VIDEO_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


@dataclass(frozen=True)
class FanhaoConfig:
    """保存当前租户绑定的番号查询配置。"""

    commands: tuple[str, ...]
    image_search_enabled: bool
    image_search_commands: tuple[str, ...]
    javbus_urls: tuple[str, ...]
    request_timeout_seconds: int
    allow_download_link: bool
    prefer_subtitle_magnet: bool
    avscan_base_url: str
    avscan_timeout_seconds: int
    avscan_result_count: int
    avscan_min_similarity: float
    avscan_fetch_detail: bool
    points_cost: int
    rate_limit: int
    rate_window_seconds: int
    max_image_bytes: int


@dataclass(frozen=True)
class MovieDetail:
    code: str
    title: str
    image_url: str
    date: str
    length_minutes: int | None
    publisher: str
    stars: tuple[str, ...]
    gid: str
    uc: str
    source_url: str


@dataclass(frozen=True)
class ImageMatch:
    code: str
    similarity: float
    timestamp: str


def load_config(raw_config: Mapping[str, Any]) -> FanhaoConfig:
    """读取配置并固定站点、结果数量、超时和图片大小上限。"""

    raw = dict(raw_config or {})
    primary = _normalize_base_url(raw.get("javbus_base_url"), DEFAULT_JAVBUS_URL)
    fallback_values = raw.get("javbus_fallback_base_urls", DEFAULT_JAVBUS_FALLBACK_URLS)
    if isinstance(fallback_values, str):
        fallback_values = [fallback_values]
    urls: list[str] = [primary]
    if isinstance(fallback_values, (list, tuple)):
        for item in fallback_values:
            normalized = _normalize_base_url(item, "")
            if normalized and normalized not in urls:
                urls.append(normalized)
    avscan = _normalize_base_url(raw.get("avscan_base_url"), DEFAULT_AVSCAN_URL)
    return FanhaoConfig(
        commands=_commands(raw.get("commands"), DEFAULT_COMMANDS),
        image_search_enabled=bool(raw.get("image_search_enabled", True)),
        image_search_commands=_commands(raw.get("image_search_commands"), DEFAULT_IMAGE_COMMANDS),
        javbus_urls=tuple(urls),
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=8, minimum=1, maximum=120),
        allow_download_link=bool(raw.get("allow_download_link", True)),
        prefer_subtitle_magnet=bool(raw.get("prefer_subtitle_magnet", True)),
        avscan_base_url=avscan,
        avscan_timeout_seconds=_bounded_int(raw.get("avscan_timeout_seconds"), default=20, minimum=1, maximum=120),
        avscan_result_count=_bounded_int(raw.get("avscan_result_count"), default=2, minimum=1, maximum=2),
        avscan_min_similarity=max(0.0, min(float(raw.get("avscan_min_similarity", 60) or 60), 100.0)),
        avscan_fetch_detail=bool(raw.get("avscan_fetch_detail", True)),
        points_cost=_bounded_int(raw.get("points_cost"), default=100, minimum=0, maximum=100_000),
        rate_limit=_bounded_int(raw.get("rate_limit"), default=3, minimum=1, maximum=100),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), default=60, minimum=1, maximum=86_400),
        max_image_bytes=_bounded_int(
            raw.get("max_image_bytes"),
            default=8 * 1024 * 1024,
            minimum=100,
            maximum=8 * 1024 * 1024,
        ),
    )


def normalize_code(value: str) -> str:
    """规范化番号并拒绝无法安全展示的字符。"""

    normalized = re.sub(r"([A-Za-z])([0-9])", r"\1-\2", str(value or "").strip().upper())
    return normalized if VIDEO_CODE_PATTERN.fullmatch(normalized) else ""


def fetch_movie_detail(client: PluginHttpClient, config: FanhaoConfig, code: str) -> MovieDetail | None:
    """按主站到备用站顺序查询详情，网络失败时才切换站点。"""

    normalized_code = normalize_code(code)
    if not normalized_code:
        return None
    for base_url in config.javbus_urls:
        detail_url = f"{base_url}/{normalized_code}"
        try:
            response = client.request(
                "GET",
                detail_url,
                headers=_javbus_headers(),
                connect_timeout_seconds=min(8, config.request_timeout_seconds),
                total_timeout_seconds=config.request_timeout_seconds,
                max_response_bytes=4 * 1024 * 1024,
                max_redirects=2,
                retry_count=1,
            )
        except PluginHttpError:
            continue
        if response.status_code < 200 or response.status_code >= 300:
            continue
        detail = parse_movie_detail(response.text, normalized_code, base_url=base_url)
        if detail is not None:
            return detail
    return None


def fetch_best_magnet(client: PluginHttpClient, config: FanhaoConfig, detail: MovieDetail) -> str:
    """从成功站点的 AJAX 接口取磁力并按字幕/体积策略选一条。"""

    if not detail.gid or not detail.uc:
        return ""
    url = f"{detail.source_url}/ajax/uncledatoolsbyajax.php?{urlencode({'lang': 'zh', 'gid': detail.gid, 'uc': detail.uc})}"
    try:
        response = client.request(
            "GET",
            url,
            headers=_javbus_headers(f"{detail.source_url}/{detail.code}"),
            connect_timeout_seconds=min(8, config.request_timeout_seconds),
            total_timeout_seconds=config.request_timeout_seconds,
            max_response_bytes=2 * 1024 * 1024,
            max_redirects=2,
            retry_count=1,
        )
    except PluginHttpError:
        return ""
    magnets = parse_magnets(response.text)
    if config.prefer_subtitle_magnet:
        subtitle = [item for item in magnets if item["subtitle"]]
        if subtitle:
            magnets = subtitle
    return str(magnets[0]["url"]) if magnets else ""


def search_image(client: PluginHttpClient, config: FanhaoConfig, image_bytes: bytes) -> tuple[ImageMatch, ...]:
    """使用受限 multipart 请求访问 AVScan，不落盘、不保存上游原始响应。"""

    try:
        normalized, mime_type = validate_media_bytes(
            image_bytes,
            declared_type=None,
            allowed_types=IMAGE_TYPES,
            max_bytes=config.max_image_bytes,
        )
    except MediaBoundaryError as exc:
        raise ValueError("引用图片不可读或过大") from exc
    boundary = f"----MultiBotSaaS{uuid.uuid4().hex}"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("ascii"))
    body.extend(b'Content-Disposition: form-data; name="file"; filename="image.jpg"\r\n')
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"))
    body.extend(normalized)
    body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
    response = client.request(
        "POST",
        f"{config.avscan_base_url}/search",
        headers={"Accept": "application/json", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=bytes(body),
        connect_timeout_seconds=min(8, config.avscan_timeout_seconds),
        total_timeout_seconds=config.avscan_timeout_seconds,
        max_response_bytes=2 * 1024 * 1024,
        max_redirects=2,
        retry_count=0,
    )
    if response.status_code == 429:
        raise ValueError("以图查番请求太频繁")
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError("以图查番服务暂不可用")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("以图查番服务返回无效结果") from exc
    return parse_image_matches(payload, config)


def parse_movie_detail(markup: str, code: str, *, base_url: str) -> MovieDetail | None:
    """从 JavBus 页面提取最小详情，避免引入站点专用 ORM 或旧解析依赖。"""

    content = str(markup or "")
    # 真实页面容器是 class="row movie",按 class 词匹配而不是整串相等。
    if not content or not re.search(r"class=[\"'][^\"']*\bmovie\b", content):
        return None
    title = _first_text(re.search(r"<h3[^>]*>(.*?)</h3>", content, re.IGNORECASE | re.DOTALL))
    image_match = re.search(r"<(?:div|a)[^>]*class=[\"'][^\"']*bigImage[^\"']*[\"'][^>]*>.*?<img[^>]+src=[\"']([^\"']+)", content, re.IGNORECASE | re.DOTALL)
    image_url = urljoin(base_url, html_lib.unescape(image_match.group(1).strip())) if image_match else ""
    date = _label_value(content, ("發行日期", "发行日期"))
    length_text = _label_value(content, ("長度", "长度"))
    length_match = re.search(r"(\d+)", length_text)
    publisher = _label_value(content, ("發行商", "发行商"))
    # 真实页面的演员在 class="star-name" 的链接里,不在带"演員"字样的 <p> 中。
    stars = tuple(dict.fromkeys(re.findall(r'class=["\']star-name["\'][^>]*>\s*<a[^>]*>([^<]{1,80})</a>', content)))
    gid_match = re.search(r"\bvar\s+gid\s*=\s*[\"']?(\d+)", content)
    uc_match = re.search(r"\bvar\s+uc\s*=\s*[\"']?(\d+)", content)
    return MovieDetail(
        code=code,
        title=title,
        image_url=image_url,
        date=date,
        length_minutes=int(length_match.group(1)) if length_match else None,
        publisher=publisher,
        stars=tuple(star.strip() for star in stars if star.strip()),
        gid=gid_match.group(1) if gid_match else "",
        uc=uc_match.group(1) if uc_match else "",
        source_url=base_url,
    )


def parse_magnets(markup: str) -> list[dict[str, Any]]:
    """解析磁力链接、字幕标记和文件大小，并按大小降序排列。"""

    result: list[dict[str, Any]] = []
    for match in re.finditer(r"<tr[^>]*>(.*?)</tr>", str(markup or ""), re.IGNORECASE | re.DOTALL):
        row = match.group(1)
        link_match = re.search(r"href=[\"'](magnet:\?xt=urn:btih:[^\"']+)", row, re.IGNORECASE)
        if not link_match:
            continue
        plain = _strip_html(row)
        size_match = re.search(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)", plain, re.IGNORECASE)
        result.append(
            {
                "url": html_lib.unescape(link_match.group(1)),
                "subtitle": "字幕" in plain,
                "size": _size_to_bytes(size_match.group(0)) if size_match else 0.0,
            }
        )
    result.sort(key=lambda item: float(item["size"]), reverse=True)
    return result


def parse_image_matches(payload: Any, config: FanhaoConfig) -> tuple[ImageMatch, ...]:
    """只保留合法番号和达到相似度阈值的前 N 条结果。"""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
        raise ValueError("以图查番服务返回无效结果")
    matches: list[ImageMatch] = []
    seen: set[str] = set()
    for item in payload["results"]:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("video_code") or "").strip().upper()
        if not VIDEO_CODE_PATTERN.fullmatch(code) or code in seen:
            continue
        frames = item.get("frames") if isinstance(item.get("frames"), list) else []
        first = frames[0] if frames and isinstance(frames[0], Mapping) else {}
        try:
            similarity = float(item.get("best_similarity", first.get("similarity", 0)))
        except (TypeError, ValueError):
            similarity = 0.0
        similarity = max(0.0, min(100.0, similarity))
        if similarity < config.avscan_min_similarity:
            continue
        timestamp = _frame_timestamp(first.get("image_name"))
        seen.add(code)
        matches.append(ImageMatch(code=code, similarity=similarity, timestamp=timestamp))
        if len(matches) >= config.avscan_result_count:
            break
    return tuple(matches)


def format_movie_result(detail: MovieDetail, magnet: str = "") -> str:
    """生成不包含站点原始 HTML 的稳定详情文本。"""

    lines = [f"查询成功：{detail.code}", f"标题：{detail.title or '未提供标题'}"]
    if detail.date:
        lines.append(f"发行日期：{detail.date}")
    if detail.length_minutes:
        lines.append(f"片长：{detail.length_minutes} 分钟")
    if detail.publisher:
        lines.append(f"发行商：{detail.publisher}")
    if detail.stars:
        lines.append(f"女优：{', '.join(detail.stars)}")
    if magnet:
        lines.append(f"磁力：{magnet}")
    return "\n".join(lines)


def format_image_result(matches: tuple[ImageMatch, ...], detail: MovieDetail | None = None) -> str:
    """生成以图查番结果和后续详情指引。"""

    lines = ["以图查番结果："]
    for index, item in enumerate(matches, start=1):
        medal = "🥇" if index == 1 else "🥈"
        timestamp = f"｜画面 {item.timestamp}" if item.timestamp else ""
        lines.append(f"{medal} {item.code}｜相似度 {item.similarity:.1f}%{timestamp}")
    if detail is not None:
        if detail.title:
            lines.append(f"标题：{detail.title}")
        if detail.date:
            lines.append(f"发行日期：{detail.date}")
        if detail.stars:
            lines.append(f"女优：{', '.join(detail.stars)}")
    lines.append(f"如需详情可发送：番号 {matches[0].code}")
    return "\n".join(lines)


def _label_value(content: str, labels: tuple[str, ...]) -> str:
    text = _strip_html(_label_block(content, labels)).strip()
    for label in labels:
        text = re.sub(rf"^{re.escape(label)}\s*[:：]?\s*", "", text, count=1)
    return text.strip()


def _label_block(content: str, labels: tuple[str, ...]) -> str:
    label_pattern = "|".join(re.escape(item) for item in labels)
    for match in re.finditer(r"<p[^>]*>.*?</p>", content, re.IGNORECASE | re.DOTALL):
        block = match.group(0)
        if re.search(label_pattern, block, re.IGNORECASE):
            return block
    return ""


def _first_text(match: re.Match[str] | None) -> str:
    return _strip_html(match.group(1)) if match else ""


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_lib.unescape(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()


def _size_to_bytes(value: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)", str(value or ""), re.IGNORECASE)
    if not match:
        return 0.0
    return float(match.group(1)) * {"KB": 1024.0, "MB": 1024.0**2, "GB": 1024.0**3, "TB": 1024.0**4}[match.group(2).upper()]


def _frame_timestamp(value: Any) -> str:
    name = str(value or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if "_cover_" in name:
        return "封面"
    if "_" not in name:
        return ""
    timestamp = name.rsplit("_", 1)[-1].replace("-", ":")
    return timestamp if re.fullmatch(r"\d{1,3}(?::\d{1,2}){1,2}", timestamp) else ""


def _normalize_base_url(value: Any, fallback: str) -> str:
    raw = str(value or fallback).strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
        return fallback
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/') }"


def _commands(value: Any, defaults: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return defaults
    normalized = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    return normalized or defaults


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _user_agent() -> str:
    """JavBus 对非浏览器 UA 容易返回拦截页，沿用 abot 验证可用的浏览器 UA。"""

    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    )


def _javbus_headers(referer: str = "") -> dict[str, str]:
    """JavBus 请求头:带上 dv=1 年龄确认 Cookie,否则站点会 302 到 /doc/driver-verify 验证页。"""

    headers = {"User-Agent": _user_agent(), "Accept": "text/html", "Cookie": "dv=1"}
    if referer:
        headers["Referer"] = referer
    return headers

