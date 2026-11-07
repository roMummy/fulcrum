from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.services.plugin_runtime.http_client import PluginHttpClient
from plugins._shared.media import (
    IMAGE_TYPES,
    VIDEO_TYPES,
    MediaBoundaryError,
    download_media,
    extract_video_cover,
    probe_video_duration,
    validate_media_bytes,
)
from plugins._shared.resources import ResourceBoundaryError, create_tenant_temp_dir, safe_remove_tree


DEFAULT_VIDEO_URL = "http://api.yujn.cn/api/heisis.php?type=video"
DEFAULT_COMMANDS = ("黑丝视频", "黑丝", "来个黑丝", "搞个黑丝")


@dataclass(frozen=True)
class VideoConfig:
    """保存当前租户绑定的视频下载和资源限制。"""

    enabled: bool
    commands: tuple[str, ...]
    video_url: str
    request_timeout_seconds: int
    points_cost: int
    rate_limit: int
    rate_window_seconds: int
    max_video_bytes: int
    max_video_seconds: int


@dataclass(frozen=True)
class VideoResult:
    """仅保存标准视频回复需要的编码内容和时长。"""

    video_base64: str
    cover_base64: str
    duration_seconds: int | None


def load_config(raw_config: Mapping[str, Any]) -> VideoConfig:
    """读取视频插件配置，限制地址、超时、媒体和积分边界。"""

    raw = dict(raw_config or {})
    commands = _commands(raw.get("commands"))
    video_url = str(raw.get("video_url") or DEFAULT_VIDEO_URL).strip()
    if not video_url.startswith(("http://", "https://")):
        video_url = DEFAULT_VIDEO_URL
    max_video_bytes = _bounded_int(
        raw.get("max_video_bytes"),
        default=40 * 1024 * 1024,
        minimum=1,
        maximum=40 * 1024 * 1024,
    )
    return VideoConfig(
        enabled=bool(raw.get("enabled", False)),
        commands=commands,
        video_url=video_url,
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=20, minimum=1, maximum=300),
        points_cost=_bounded_int(raw.get("points_cost"), default=8, minimum=0, maximum=100_000),
        rate_limit=_bounded_int(raw.get("rate_limit"), default=3, minimum=1, maximum=100),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), default=60, minimum=1, maximum=86_400),
        max_video_bytes=max_video_bytes,
        max_video_seconds=_bounded_int(raw.get("max_video_seconds"), default=180, minimum=1, maximum=86_400),
    )


def fetch_video(client: PluginHttpClient, tenant_id: int, config: VideoConfig) -> VideoResult:
    """下载视频、生成首帧、检查时长并在返回前清理租户临时目录。"""

    temp_dir = create_tenant_temp_dir(tenant_id, "video")
    video_path = temp_dir / "video.bin"
    cover_path = temp_dir / "cover.jpg"
    try:
        video_bytes, _ = download_media(
            client,
            config.video_url,
            allowed_types=VIDEO_TYPES | {"application/octet-stream"},
            max_bytes=config.max_video_bytes,
            timeout_seconds=config.request_timeout_seconds,
        )
        video_path.write_bytes(video_bytes)
        duration = probe_video_duration(video_path)
        if duration is not None and duration > config.max_video_seconds:
            raise MediaBoundaryError("视频时长超过上限")
        extract_video_cover(video_path, cover_path)
        cover_bytes = cover_path.read_bytes()
        validate_media_bytes(
            cover_bytes,
            declared_type="image/jpeg",
            allowed_types=IMAGE_TYPES,
            max_bytes=2 * 1024 * 1024,
        )
        return VideoResult(
            video_base64=base64.b64encode(video_bytes).decode("ascii"),
            cover_base64=base64.b64encode(cover_bytes).decode("ascii"),
            duration_seconds=int(duration) if duration is not None else None,
        )
    except (OSError, MediaBoundaryError, ResourceBoundaryError) as exc:
        raise ValueError("视频资源暂时不可用") from exc
    finally:
        try:
            safe_remove_tree(temp_dir)
        except ResourceBoundaryError:
            # 临时路径由平台生成；清理失败不把本地绝对路径暴露给用户。
            pass


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

