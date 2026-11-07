from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urljoin

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, validate_http_url
from plugins._shared.media import IMAGE_TYPES, MediaBoundaryError, validate_media_bytes
from plugins._shared.resources import ResourceBoundaryError, iter_owned_files, read_owned_bytes, tenant_media_root


STATE_NAMESPACE = "xiuren_image"
STATE_KEY = "cache"
DEFAULT_COMMANDS = ("图来", "秀人", "美图", "随机图片")
IMAGE_URL_PATTERN = re.compile(r"https?://[^\"'<>\s]+?\.(?:jpe?g|png|gif|webp)(?:\?[^\"'<>\s]*)?", re.IGNORECASE)


@dataclass(frozen=True)
class XiurenConfig:
    """保存当前租户绑定的图片缓存和抓取限制。"""

    enabled: bool
    commands: tuple[str, ...]
    resource_dir: str
    cache_size: int
    points_cost: int
    rate_limit: int
    rate_window_seconds: int
    source_urls: tuple[str, ...]
    refresh_enabled: bool
    refresh_interval_seconds: int
    max_refresh_images: int
    request_timeout_seconds: int
    max_image_bytes: int


@dataclass(frozen=True)
class CachedImage:
    """缓存状态中可安全保存的图片元数据。"""

    relative_path: str
    digest: str
    source_url: str


def load_config(raw_config: Mapping[str, Any]) -> XiurenConfig:
    """读取并限制租户图片缓存配置，不接受绝对路径或无限抓取。"""

    raw = dict(raw_config or {})
    resource_dir = str(raw.get("resource_dir") or "images").strip()
    path = Path(resource_dir)
    if not resource_dir or path.is_absolute() or ".." in path.parts:
        resource_dir = "images"
    source_urls: list[str] = []
    raw_sources = raw.get("source_urls")
    if isinstance(raw_sources, str):
        raw_sources = [raw_sources]
    if isinstance(raw_sources, (list, tuple)):
        for value in raw_sources:
            try:
                normalized = validate_http_url(str(value or "").strip())
            except Exception:
                continue
            if normalized not in source_urls:
                source_urls.append(normalized)
    return XiurenConfig(
        enabled=bool(raw.get("enabled", False)),
        commands=_commands(raw.get("commands")),
        resource_dir=resource_dir,
        cache_size=_bounded_int(raw.get("cache_size"), default=10, minimum=1, maximum=100),
        points_cost=_bounded_int(raw.get("points_cost"), default=5, minimum=0, maximum=100_000),
        rate_limit=_bounded_int(raw.get("rate_limit"), default=5, minimum=1, maximum=100),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), default=60, minimum=1, maximum=86_400),
        source_urls=tuple(source_urls),
        refresh_enabled=bool(raw.get("refresh_enabled", False)),
        refresh_interval_seconds=_bounded_int(raw.get("refresh_interval_seconds"), default=86_400, minimum=300, maximum=2_592_000),
        max_refresh_images=_bounded_int(raw.get("max_refresh_images"), default=20, minimum=1, maximum=200),
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=20, minimum=1, maximum=300),
        max_image_bytes=_bounded_int(raw.get("max_image_bytes"), default=8 * 1024 * 1024, minimum=1, maximum=8 * 1024 * 1024),
    )


def load_cached_images(tenant_id: int, config: XiurenConfig, state: Mapping[str, Any] | None) -> tuple[CachedImage, ...]:
    """只返回状态中仍存在且位于当前租户目录内的图片。"""

    raw_items = state.get("files") if isinstance(state, Mapping) else []
    if not isinstance(raw_items, list):
        return ()
    root = tenant_media_root(tenant_id, "xiuren_image") / config.resource_dir
    if not root.exists():
        return ()
    valid: list[CachedImage] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        relative_path = str(item.get("path") or "").strip()
        digest = str(item.get("digest") or "").strip()
        source_url = str(item.get("source_url") or "").strip()
        if not relative_path or not digest:
            continue
        try:
            candidate = (root / relative_path).resolve(strict=True)
            candidate.relative_to(root.resolve(strict=True))
            if candidate.is_file():
                valid.append(CachedImage(relative_path=relative_path, digest=digest, source_url=source_url))
        except (OSError, ValueError):
            continue
    return tuple(valid[: config.cache_size])


def choose_cached_image(tenant_id: int, config: XiurenConfig, state: Mapping[str, Any] | None, *, chooser=None) -> tuple[bytes, CachedImage] | None:
    """从当前绑定缓存中随机读取图片并重新校验大小和类型。"""

    items = load_cached_images(tenant_id, config, state)
    if not items:
        return None
    select = chooser or random.choice
    selected = select(items)
    root = tenant_media_root(tenant_id, "xiuren_image") / config.resource_dir
    try:
        data = read_owned_bytes(root / selected.relative_path, max_bytes=config.max_image_bytes)
        normalized, _ = validate_media_bytes(data, declared_type=None, allowed_types=IMAGE_TYPES, max_bytes=config.max_image_bytes)
    except (ResourceBoundaryError, MediaBoundaryError):
        return None
    return normalized, selected


def refresh_cached_images(client: PluginHttpClient, tenant_id: int, config: XiurenConfig, state: Mapping[str, Any] | None) -> dict[str, Any]:
    """抓取配置站点中的有限图片链接，失败时保留旧缓存。"""

    if not config.source_urls:
        return {"files": list((state or {}).get("files") or []), "downloaded": 0, "failed": 0}
    root = tenant_media_root(tenant_id, "xiuren_image") / config.resource_dir
    root.mkdir(parents=True, exist_ok=True)
    links: list[str] = []
    for source_url in config.source_urls:
        try:
            response = client.request(
                "GET",
                source_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; MultiBotSaaS-Xiuren/2.0)", "Accept": "text/html,image/*"},
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
        for raw_link in IMAGE_URL_PATTERN.findall(response.text):
            absolute = urljoin(source_url, raw_link)
            if absolute not in links:
                links.append(absolute)
            if len(links) >= config.max_refresh_images:
                break
        if len(links) >= config.max_refresh_images:
            break

    old_items = list((state or {}).get("files") or []) if isinstance(state, Mapping) else []
    by_digest: dict[str, dict[str, str]] = {}
    for item in old_items:
        if isinstance(item, Mapping) and str(item.get("digest") or "").strip():
            by_digest[str(item["digest"])] = {"path": str(item.get("path") or ""), "digest": str(item["digest"]), "source_url": str(item.get("source_url") or "")}
    downloaded = failed = 0
    for link in links:
        try:
            response = client.request(
                "GET",
                validate_http_url(link),
                headers={"User-Agent": "Mozilla/5.0 (compatible; MultiBotSaaS-Xiuren/2.0)", "Accept": "image/*"},
                connect_timeout_seconds=min(8, config.request_timeout_seconds),
                total_timeout_seconds=config.request_timeout_seconds,
                max_response_bytes=config.max_image_bytes,
                max_redirects=2,
                retry_count=1,
            )
            if response.status_code < 200 or response.status_code >= 300:
                failed += 1
                continue
            content, _ = validate_media_bytes(response.body, declared_type=response.headers.get("content-type"), allowed_types=IMAGE_TYPES, max_bytes=config.max_image_bytes)
            digest = hashlib.sha256(content).hexdigest()
            relative_path = f"{digest}.img"
            (root / relative_path).write_bytes(content)
            by_digest[digest] = {"path": relative_path, "digest": digest, "source_url": link}
            downloaded += 1
        except (OSError, PluginHttpError, MediaBoundaryError, ValueError):
            failed += 1
    files = list(by_digest.values())[: config.cache_size]
    return {"files": files, "downloaded": downloaded, "failed": failed}


def _commands(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return DEFAULT_COMMANDS
    normalized = tuple(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    return normalized or DEFAULT_COMMANDS


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))

