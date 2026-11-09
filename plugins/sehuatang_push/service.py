from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, validate_http_url
from plugins._shared.resources import create_tenant_temp_dir, safe_remove_tree

from plugins.sehuatang_push.pdf_renderer import build_pdf_html, render_html_to_pdf


DEFAULT_SOURCE_URLS = ("https://www.javbus.com",)


@dataclass(frozen=True)
class SehuatangConfig:
    """保存当前租户绑定的抓取、PDF 和推送限制。"""

    source_urls: tuple[str, ...]
    push_time: str
    target_conversation_ids: tuple[int, ...]
    request_timeout_seconds: int
    max_pages: int
    max_movies: int
    max_pdf_bytes: int
    retention_seconds: int
    title_prefix: str


@dataclass(frozen=True)
class Movie:
    code: str
    title: str
    release_date: str
    detail_url: str


def load_config(raw_config: Mapping[str, Any]) -> SehuatangConfig:
    """读取配置并限制页面数、影片数、文件大小和目标会话。"""

    raw = dict(raw_config or {})
    sources: list[str] = []
    values = raw.get("source_urls")
    if isinstance(values, str):
        values = [values]
    if isinstance(values, (list, tuple)):
        for value in values:
            try:
                normalized = validate_http_url(str(value or "").strip()).rstrip("/")
            except Exception:
                continue
            if normalized not in sources:
                sources.append(normalized)
    if not sources:
        sources = list(DEFAULT_SOURCE_URLS)
    ids: list[int] = []
    values = raw.get("target_conversation_ids")
    if isinstance(values, (list, tuple)):
        for value in values:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0 and number not in ids:
                ids.append(number)
    return SehuatangConfig(
        source_urls=tuple(sources),
        push_time=str(raw.get("push_time") or "15:30").strip() or "15:30",
        target_conversation_ids=tuple(ids),
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=15, minimum=1, maximum=120),
        max_pages=_bounded_int(raw.get("max_pages"), default=10, minimum=1, maximum=50),
        max_movies=_bounded_int(raw.get("max_movies"), default=120, minimum=1, maximum=500),
        max_pdf_bytes=_bounded_int(raw.get("max_pdf_bytes"), default=16 * 1024 * 1024, minimum=1, maximum=32 * 1024 * 1024),
        retention_seconds=_bounded_int(raw.get("retention_seconds"), default=86_400, minimum=300, maximum=31_536_000),
        title_prefix=str(raw.get("title_prefix") or "今日新片目录").strip()[:120] or "今日新片目录",
    )


def collect_movies(client: PluginHttpClient, config: SehuatangConfig) -> tuple[Movie, ...]:
    """按站点顺序抓取有限列表页，站点不可用时切换下一个候选地址。"""

    for base_url in config.source_urls:
        movies: list[Movie] = []
        try:
            for page in range(1, config.max_pages + 1):
                url = base_url if page == 1 else f"{base_url}/page/{page}"
                response = client.request(
                    "GET",
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; MultiBotSaaS-Sehuatang/2.0)", "Accept": "text/html"},
                    connect_timeout_seconds=min(8, config.request_timeout_seconds),
                    total_timeout_seconds=config.request_timeout_seconds,
                    max_response_bytes=4 * 1024 * 1024,
                    max_redirects=2,
                    retry_count=1,
                )
                if response.status_code < 200 or response.status_code >= 300:
                    raise PluginHttpError("列表页请求失败")
                page_movies = parse_movie_list(response.text, base_url)
                movies.extend(page_movies)
                if len(movies) >= config.max_movies or not page_movies:
                    break
            if movies:
                return tuple(movies[: config.max_movies])
        except PluginHttpError:
            continue
    return ()


def parse_movie_list(markup: str, base_url: str) -> tuple[Movie, ...]:
    """解析 JavBus 列表卡片的编号、标题、日期和详情地址。"""

    result: list[Movie] = []
    for block in re.findall(r"<a[^>]+class=[\"'][^\"']*movie-box[^\"']*[\"'][^>]*>.*?</a>", str(markup or ""), re.IGNORECASE | re.DOTALL):
        dates = re.findall(r"<date[^>]*>\s*([^<]+?)\s*</date>", block, re.IGNORECASE)
        if len(dates) < 2:
            continue
        code = str(dates[0]).strip()
        release_date = str(dates[1]).strip()
        if not code or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
            continue
        href_match = re.search(r"href=[\"']([^\"']+)", block, re.IGNORECASE)
        title_match = re.search(r"title=[\"']([^\"']+)", block, re.IGNORECASE)
        result.append(
            Movie(
                code=code,
                title=re.sub(r"\s+", " ", str(title_match.group(1) if title_match else "")).strip(),
                release_date=release_date,
                detail_url=urljoin(base_url + "/", href_match.group(1).strip()) if href_match else base_url,
            )
        )
    return tuple(result)


def build_pdf(client: PluginHttpClient, tenant_id: int, config: SehuatangConfig) -> tuple[Path, int, str]:
    """抓取目录并生成租户临时 PDF，返回文件、影片数量和稳定摘要。"""

    temp_dir = create_tenant_temp_dir(tenant_id, "sehuatang_push")
    try:
        movies = collect_movies(client, config)
        if not movies:
            raise ValueError("没有抓到可用影片")
        payload = [movie.__dict__ for movie in movies]
        digest = hashlib.sha256("|".join(f"{item.code}:{item.release_date}" for item in movies).encode("utf-8")).hexdigest()
        html_content = build_pdf_html(f"{config.title_prefix}（{len(movies)}部）", payload)
        output_path = render_html_to_pdf(html_content, temp_dir / "daily-catalog.pdf")
        if output_path.stat().st_size > config.max_pdf_bytes:
            raise ValueError("生成的 PDF 超过大小上限")
        return output_path, len(movies), digest
    except Exception:
        try:
            safe_remove_tree(temp_dir)
        except Exception:
            pass
        raise


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))

