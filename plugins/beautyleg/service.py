from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from plugins._shared.media import IMAGE_TYPES, MediaBoundaryError, read_media_base64
from plugins._shared.resources import ResourceBoundaryError, iter_owned_files, tenant_media_root


DEFAULT_COMMANDS = ("美腿", "腿来", "腿照", "来张美腿图片")
DEFAULT_RESOURCE_DIR = "images"


@dataclass(frozen=True)
class BeautyLegConfig:
    """保存当前租户绑定唯一生效的图片插件配置。"""

    commands: tuple[str, ...]
    resource_dir: str
    points_cost: int
    rate_limit: int
    rate_window_seconds: int
    max_image_bytes: int


def load_config(raw_config: Mapping[str, Any]) -> BeautyLegConfig:
    """读取并收敛配置，拒绝绝对路径和不合理的资源/限流上限。"""

    raw = dict(raw_config or {})
    commands = _commands(raw.get("commands"))
    resource_dir = str(raw.get("resource_dir") or DEFAULT_RESOURCE_DIR).strip()
    resource_path = Path(resource_dir)
    if not resource_dir or resource_path.is_absolute() or ".." in resource_path.parts:
        resource_dir = DEFAULT_RESOURCE_DIR
    return BeautyLegConfig(
        commands=commands,
        resource_dir=resource_dir,
        points_cost=_bounded_int(raw.get("points_cost"), default=2, minimum=0, maximum=100_000),
        rate_limit=_bounded_int(raw.get("rate_limit"), default=1, minimum=1, maximum=100),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), default=60, minimum=1, maximum=86_400),
        max_image_bytes=_bounded_int(
            raw.get("max_image_bytes"),
            default=16 * 1024 * 1024,
            minimum=1,
            maximum=16 * 1024 * 1024,
        ),
    )


def select_random_image(tenant_id: int, config: BeautyLegConfig, *, chooser=None) -> Path | None:
    """从当前租户插件目录中选择图片，不扫描旧项目路径或共享目录。"""

    root = tenant_media_root(tenant_id, "beautyleg") / Path(config.resource_dir)
    if not root.exists() or not root.is_dir():
        return None
    files = iter_owned_files(root, suffixes={".jpg", ".jpeg", ".png", ".gif", ".webp"})
    if not files:
        return None
    if chooser is None:
        import random

        chooser = random.choice
    return Path(chooser(files))


def build_image_reply(path: Path, config: BeautyLegConfig) -> dict[str, str]:
    """把租户目录中的图片转换为平台标准图片回复。"""

    try:
        encoded, _ = read_media_base64(
            path,
            allowed_root=path.parents[len(Path(config.resource_dir).parts)] if Path(config.resource_dir).parts else path.parent,
            max_bytes=config.max_image_bytes,
            allowed_types=IMAGE_TYPES,
        )
    except (MediaBoundaryError, ResourceBoundaryError) as exc:
        raise ValueError("图片资源不可用") from exc
    return {"type": "image", "base64": encoded}


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

