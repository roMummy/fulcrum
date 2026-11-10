from __future__ import annotations

import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.core.settings import settings
from plugins._shared.media import IMAGE_TYPES, MediaBoundaryError, validate_media_bytes
from plugins._shared.resources import ResourceBoundaryError, read_owned_bytes, tenant_media_root


STATE_NAMESPACE = "xiuren_image"
STATE_KEY = "pool"
DEFAULT_COMMANDS = ("图来", "秀人", "美图", "随机图片")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
# 扫描目录时最多看多少个文件;目录特别大时只取前面这些用于随机抽样。
MAX_SCANNED_FILES = 20_000


@dataclass(frozen=True)
class XiurenConfig:
    """图片目录与发送限制。"""

    commands: tuple[str, ...]
    library_dir: str
    prefetch_count: int
    points_cost: int
    rate_limit: int
    rate_window_seconds: int
    max_image_bytes: int


def load_config(raw_config: Mapping[str, Any]) -> XiurenConfig:
    """读取并限制配置:图片目录必须是相对路径,不能越出图片库根目录。"""

    raw = dict(raw_config or {})
    library_dir = str(raw.get("library_dir") or "xiuren").strip().strip("/")
    path = Path(library_dir)
    if not library_dir or path.is_absolute() or ".." in path.parts:
        library_dir = "xiuren"
    return XiurenConfig(
        commands=_commands(raw.get("commands")),
        library_dir=library_dir,
        prefetch_count=_bounded_int(raw.get("prefetch_count"), default=20, minimum=1, maximum=100),
        points_cost=_bounded_int(raw.get("points_cost"), default=5, minimum=0, maximum=100_000),
        rate_limit=_bounded_int(raw.get("rate_limit"), default=5, minimum=1, maximum=100),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), default=60, minimum=1, maximum=86_400),
        max_image_bytes=_bounded_int(raw.get("max_image_bytes"), default=8 * 1024 * 1024, minimum=1, maximum=8 * 1024 * 1024),
    )


def library_root(tenant_id: int, config: XiurenConfig) -> Path:
    """图片所在目录。

    平台配置了图片库根目录(MEDIA_LIBRARY_ROOT,通常是挂载进容器的共享盘)就用它下面的子目录;
    没配置时用本租户自己的插件目录。两种情况都只读取该目录内的文件。
    """

    base = str(settings.media_library_root or "").strip()
    root = Path(base) / config.library_dir if base else tenant_media_root(tenant_id, "xiuren_image") / config.library_dir
    return root


def scan_images(root: Path, config: XiurenConfig) -> list[str]:
    """列出目录下的图片(相对路径),过滤非图片、空文件、超限文件和越界符号链接。"""

    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return []
    found: list[str] = []
    stack = [resolved_root]
    while stack and len(found) < MAX_SCANNED_FILES:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.name.lower().endswith(IMAGE_SUFFIXES) or not entry.is_file():
                    continue
                size = entry.stat().st_size
                if not 0 < size <= config.max_image_bytes:
                    continue
                # 符号链接指到目录外的文件一律忽略。
                Path(entry.path).resolve(strict=True).relative_to(resolved_root)
                found.append(str(Path(entry.path).relative_to(resolved_root)))
            except (OSError, ValueError):
                continue
            if len(found) >= MAX_SCANNED_FILES:
                break
    return found


def refill_pool(tenant_id: int, config: XiurenConfig, *, sampler=None) -> list[str]:
    """从目录里随机抽 prefetch_count 张图的路径组成新的预取池(只记路径,不复制文件)。"""

    images = scan_images(library_root(tenant_id, config), config)
    if not images:
        return []
    return list((sampler or random.sample)(images, min(config.prefetch_count, len(images))))


def pick_from_pool(
    tenant_id: int,
    config: XiurenConfig,
    pool: list[str],
    *,
    chooser=None,
) -> tuple[bytes, str, list[str]] | None:
    """从池里随机取一张并重新校验;返回 (图片, 所选路径, 去掉这张后的池),池里没有可用图返回 None。

    读不了或不是合法图片的会被丢出池子,继续换下一张。不修改磁盘上的任何文件。
    """

    root = library_root(tenant_id, config)
    candidates = list(pool)
    while candidates:
        chosen = (chooser or random.choice)(candidates)
        remaining = [item for item in candidates if item != chosen]
        try:
            path = (root / chosen).resolve(strict=True)
            path.relative_to(root.resolve(strict=True))
            data = read_owned_bytes(path, max_bytes=config.max_image_bytes)
            normalized, _ = validate_media_bytes(data, declared_type=None, allowed_types=IMAGE_TYPES, max_bytes=config.max_image_bytes)
        except (OSError, ValueError, ResourceBoundaryError, MediaBoundaryError):
            candidates = remaining
            pool = [item for item in pool if item != chosen]
            continue
        return normalized, chosen, [item for item in pool if item != chosen]
    return None


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
