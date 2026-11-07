from __future__ import annotations

import base64
import mimetypes
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError

from .resources import ResourceBoundaryError, read_owned_bytes


class MediaBoundaryError(ValueError):
    """媒体类型、大小、处理结果或临时文件违反平台边界。"""


IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
VIDEO_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/x-matroska"})
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_VIDEO_BYTES = 40 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024


def media_content_type(data: bytes, declared_type: str | None = None) -> str:
    """根据声明和少量文件签名推断媒体类型，不把任意二进制当作图片或视频。"""

    declared = str(declared_type or "").split(";", 1)[0].strip().lower()
    if declared:
        return declared
    signatures: tuple[tuple[bytes, str], ...] = (
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"RIFF", "image/webp"),
        (b"\x00\x00\x00", "video/mp4"),
    )
    for signature, content_type in signatures:
        if data.startswith(signature):
            return content_type
    return "application/octet-stream"


def validate_media_bytes(
    data: bytes,
    *,
    declared_type: str | None,
    allowed_types: Iterable[str],
    max_bytes: int,
) -> tuple[bytes, str]:
    """校验非空媒体字节、类型和大小，返回不可变副本与规范类型。"""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise MediaBoundaryError("媒体内容必须是二进制")
    normalized = bytes(data)
    if not normalized:
        raise MediaBoundaryError("媒体内容不能为空")
    try:
        limit = int(max_bytes)
    except (TypeError, ValueError) as exc:
        raise MediaBoundaryError("媒体大小上限不合法") from exc
    if limit <= 0 or len(normalized) > limit:
        raise MediaBoundaryError("媒体为空或超过大小上限")
    content_type = media_content_type(normalized, declared_type)
    allowed = {str(item).strip().lower() for item in allowed_types}
    if content_type not in allowed:
        raise MediaBoundaryError("媒体类型不受支持")
    return normalized, content_type


def download_media(
    client: PluginHttpClient,
    url: str,
    *,
    allowed_types: Iterable[str],
    max_bytes: int,
    timeout_seconds: int,
    retry_count: int = 1,
) -> tuple[bytes, str]:
    """通过平台 HTTP 客户端下载并立即校验媒体，不把响应落到共享路径。"""

    try:
        response = client.request(
            "GET",
            url,
            headers={"Accept": ", ".join(sorted(set(allowed_types)))},
            connect_timeout_seconds=min(8, int(timeout_seconds)),
            total_timeout_seconds=int(timeout_seconds),
            max_response_bytes=int(max_bytes),
            max_redirects=2,
            retry_count=retry_count,
        )
    except PluginHttpError as exc:
        raise MediaBoundaryError("媒体下载失败") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise MediaBoundaryError("媒体服务返回失败状态")
    return validate_media_bytes(
        response.body,
        declared_type=response.headers.get("content-type"),
        allowed_types=allowed_types,
        max_bytes=max_bytes,
    )


def read_media_base64(path: str | Path, *, allowed_root: str | Path, max_bytes: int, allowed_types: Iterable[str]) -> tuple[str, str]:
    """读取授权目录内媒体并编码为标准回复使用的 base64。"""

    try:
        candidate = Path(path).expanduser().resolve(strict=True)
        root = Path(allowed_root).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise MediaBoundaryError("媒体文件不存在") from exc
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise MediaBoundaryError("媒体文件不属于当前授权目录") from exc
    try:
        content = read_owned_bytes(candidate, max_bytes=max_bytes)
    except ResourceBoundaryError as exc:
        raise MediaBoundaryError(str(exc)) from exc
    content_type = media_content_type(content, mimetypes.guess_type(candidate.name)[0])
    normalized, normalized_type = validate_media_bytes(
        content,
        declared_type=content_type,
        allowed_types=allowed_types,
        max_bytes=max_bytes,
    )
    return base64.b64encode(normalized).decode("ascii"), normalized_type


def extract_video_cover(
    video_path: str | Path,
    cover_path: str | Path,
    *,
    ffmpeg_binary: str = "ffmpeg",
    runner: Callable[..., Any] | None = None,
) -> Path:
    """调用已安装的 ffmpeg 生成首帧；缺少运行依赖时明确失败，不做隐式安装或降级。"""

    source = Path(video_path).expanduser().resolve(strict=True)
    target = Path(cover_path).expanduser()
    if not source.is_file() or target.is_dir():
        raise MediaBoundaryError("视频或封面路径不合法")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg_binary),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0.1",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-f",
        "image2",
        str(target),
    ]
    execute = runner or subprocess.run
    try:
        result = execute(command, check=False, capture_output=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise MediaBoundaryError("视频封面处理依赖不可用") from exc
    if getattr(result, "returncode", 1) != 0 or not target.is_file():
        raise MediaBoundaryError("视频首帧生成失败")
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise MediaBoundaryError("视频封面读取失败") from exc
    validate_media_bytes(content, declared_type="image/jpeg", allowed_types=IMAGE_TYPES, max_bytes=MAX_COVER_BYTES)
    return target.resolve(strict=True)


def probe_video_duration(
    video_path: str | Path,
    *,
    ffprobe_binary: str = "ffprobe",
    runner: Callable[..., Any] | None = None,
) -> float | None:
    """读取视频时长；依赖缺失或容器无法解析时返回空值，由业务配置决定是否拒绝。"""

    source = Path(video_path).expanduser().resolve(strict=True)
    execute = runner or subprocess.run
    command = [
        str(ffprobe_binary),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        result = execute(command, check=False, capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        duration = float(str(getattr(result, "stdout", "") or "").strip())
    except (TypeError, ValueError):
        return None
    return duration if duration >= 0 else None


def stable_media_metadata(path: str | Path, *, content_type: str, size: int) -> Mapping[str, Any]:
    """只返回日志和状态需要的稳定媒体元数据，不包含原始内容和本地绝对路径。"""

    suffix = Path(path).suffix.lower()
    return {"content_type": str(content_type), "size": int(size), "suffix": suffix}

