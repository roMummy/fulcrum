from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Mapping

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, validate_http_url
from plugins._shared.media import IMAGE_TYPES, VIDEO_TYPES, MediaBoundaryError, extract_video_cover, probe_video_duration, validate_media_bytes
from plugins._shared.resources import ResourceBoundaryError, create_tenant_temp_dir, safe_remove_tree


DEFAULT_API_URL = "https://api.52vmy.cn/api/video/boy?type=json"
DEFAULT_COMMANDS = ("猛男", "肌肉", "帅哥")


@dataclass(frozen=True)
class VideoManConfig:
    """保存当前租户绑定的猛男视频服务配置。"""

    commands: tuple[str, ...]
    api_url: str
    request_timeout_seconds: int
    points_cost: int
    rate_limit: int
    rate_window_seconds: int
    max_video_bytes: int
    max_video_seconds: int


@dataclass(frozen=True)
class VideoManResult:
    """保存标准视频回复所需的最小结果。"""

    video_base64: str
    cover_base64: str
    duration_seconds: int | None


def load_config(raw_config: Mapping[str, Any]) -> VideoManConfig:
    """读取并限制 API、超时、媒体、积分和限流配置。"""

    raw = dict(raw_config or {})
    api_url = str(raw.get("api_url") or DEFAULT_API_URL).strip()
    if not api_url.startswith(("http://", "https://")):
        api_url = DEFAULT_API_URL
    return VideoManConfig(
        commands=_commands(raw.get("commands")),
        api_url=api_url,
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=20, minimum=1, maximum=300),
        points_cost=_bounded_int(raw.get("points_cost"), default=2, minimum=0, maximum=100_000),
        rate_limit=_bounded_int(raw.get("rate_limit"), default=3, minimum=1, maximum=100),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), default=60, minimum=1, maximum=86_400),
        max_video_bytes=_bounded_int(
            raw.get("max_video_bytes"),
            default=40 * 1024 * 1024,
            minimum=1,
            maximum=40 * 1024 * 1024,
        ),
        max_video_seconds=_bounded_int(raw.get("max_video_seconds"), default=180, minimum=1, maximum=86_400),
    )


def fetch_video(client: PluginHttpClient, tenant_id: int, config: VideoManConfig) -> VideoManResult:
    """先解析目录服务返回的视频地址，再按统一媒体边界下载并清理临时文件。"""

    temp_dir = create_tenant_temp_dir(tenant_id, "video_man")
    video_path = temp_dir / "video.bin"
    cover_path = temp_dir / "cover.jpg"
    try:
        payload = client.get_json(
            validate_http_url(config.api_url),
            connect_timeout_seconds=min(8, config.request_timeout_seconds),
            total_timeout_seconds=config.request_timeout_seconds,
            max_response_bytes=2 * 1024 * 1024,
            max_redirects=2,
            retry_count=1,
        )
        video_url = _resolve_video_url(payload)
        video_bytes, _ = _download_video(client, video_url, config)
        video_path.write_bytes(video_bytes)
        duration = probe_video_duration(video_path)
        if duration is not None and duration > config.max_video_seconds:
            raise MediaBoundaryError("视频时长超过上限")
        extract_video_cover(video_path, cover_path)
        cover_bytes = cover_path.read_bytes()
        validate_media_bytes(cover_bytes, declared_type="image/jpeg", allowed_types=IMAGE_TYPES, max_bytes=2 * 1024 * 1024)
        return VideoManResult(
            video_base64=base64.b64encode(video_bytes).decode("ascii"),
            cover_base64=base64.b64encode(cover_bytes).decode("ascii"),
            duration_seconds=int(duration) if duration is not None else None,
        )
    except (OSError, PluginHttpError, MediaBoundaryError, ResourceBoundaryError, ValueError) as exc:
        raise ValueError("猛男视频暂时不可用") from exc
    finally:
        try:
            safe_remove_tree(temp_dir)
        except ResourceBoundaryError:
            pass


def _download_video(client: PluginHttpClient, url: str, config: VideoManConfig) -> tuple[bytes, str]:
    """下载 API 返回的视频 URL，统一限制响应大小和媒体类型。"""

    from plugins._shared.media import download_media

    return download_media(
        client,
        validate_http_url(url),
        allowed_types=VIDEO_TYPES | {"application/octet-stream"},
        max_bytes=config.max_video_bytes,
        timeout_seconds=config.request_timeout_seconds,
    )


def _resolve_video_url(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("视频目录响应格式无效")
    data = payload.get("data")
    video_url = data.get("video") if isinstance(data, Mapping) else None
    normalized = str(video_url or "").strip()
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("视频目录未返回有效地址")
    return normalized


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

