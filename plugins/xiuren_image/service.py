from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from plugins._shared.media import IMAGE_TYPES, MediaBoundaryError, validate_media_bytes
from plugins._shared.resources import ResourceBoundaryError, read_owned_bytes, tenant_media_root
from plugins._shared.s3 import S3Client, S3Config, S3Error, reject_unsafe_endpoint


STATE_NAMESPACE = "xiuren_image"
STATE_KEY = "cache"
DEFAULT_COMMANDS = ("图来", "秀人", "美图", "随机图片")
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")
# 预取池里剩余张数不超过它时,发图后在后台补下一批。
LOW_WATER = 5
# 刚入队过补图任务后,这段时间内不重复入队(秒)。
REFRESH_COOLDOWN_SECONDS = 120
# 从桶里最多列出多少个对象用于随机抽样;只取前面若干页,不追求完整。
MAX_LISTED_OBJECTS = 5000


@dataclass(frozen=True)
class XiurenConfig:
    """图片来源(MinIO)与发送限制。"""

    commands: tuple[str, ...]
    resource_dir: str
    points_cost: int
    rate_limit: int
    rate_window_seconds: int
    request_timeout_seconds: int
    max_image_bytes: int
    # 每次从 MinIO 预读取的张数。
    prefetch_count: int
    minio_endpoint: str
    minio_bucket: str
    minio_prefix: str
    minio_secure: bool
    minio_region: str
    minio_access_key: str
    minio_secret_key: str

    def s3_config(self) -> S3Config:
        return S3Config(
            endpoint=self.minio_endpoint,
            bucket=self.minio_bucket,
            access_key=self.minio_access_key,
            secret_key=self.minio_secret_key,
            secure=self.minio_secure,
            region=self.minio_region,
            timeout_seconds=self.request_timeout_seconds,
        )

    def source_configured(self) -> bool:
        return self.s3_config().is_complete()


def load_config(raw_config: Mapping[str, Any]) -> XiurenConfig:
    """读取并限制配置:不接受绝对路径,预读张数与大小都有上限。"""

    raw = dict(raw_config or {})
    resource_dir = str(raw.get("resource_dir") or "images").strip()
    path = Path(resource_dir)
    if not resource_dir or path.is_absolute() or ".." in path.parts:
        resource_dir = "images"
    endpoint = str(raw.get("minio_endpoint") or "").strip()
    # 允许用户把 http:// 或 https:// 一并粘进来,协议由 minio_secure 决定。
    for scheme in ("https://", "http://"):
        if endpoint.lower().startswith(scheme):
            endpoint = endpoint[len(scheme):]
    return XiurenConfig(
        commands=_commands(raw.get("commands")),
        resource_dir=resource_dir,
        points_cost=_bounded_int(raw.get("points_cost"), default=5, minimum=0, maximum=100_000),
        rate_limit=_bounded_int(raw.get("rate_limit"), default=5, minimum=1, maximum=100),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), default=60, minimum=1, maximum=86_400),
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=20, minimum=1, maximum=300),
        max_image_bytes=_bounded_int(raw.get("max_image_bytes"), default=8 * 1024 * 1024, minimum=1, maximum=8 * 1024 * 1024),
        prefetch_count=_bounded_int(raw.get("prefetch_count"), default=20, minimum=1, maximum=50),
        minio_endpoint=endpoint.strip("/"),
        minio_bucket=str(raw.get("minio_bucket") or "").strip().strip("/"),
        minio_prefix=str(raw.get("minio_prefix") or "").strip().lstrip("/"),
        minio_secure=bool(raw.get("minio_secure", False)),
        minio_region=str(raw.get("minio_region") or "us-east-1").strip() or "us-east-1",
        minio_access_key=str(raw.get("minio_access_key") or "").strip(),
        minio_secret_key=str(raw.get("minio_secret_key") or "").strip(),
    )


def _cache_root(tenant_id: int, config: XiurenConfig) -> Path:
    return tenant_media_root(tenant_id, "xiuren_image") / config.resource_dir


def load_cached_files(tenant_id: int, config: XiurenConfig, state: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """返回状态里仍然存在于当前租户目录内的预取图片记录。"""

    raw_items = state.get("files") if isinstance(state, Mapping) else []
    if not isinstance(raw_items, list):
        return []
    root = _cache_root(tenant_id, config)
    if not root.exists():
        return []
    valid: list[dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        relative_path = str(item.get("path") or "").strip()
        digest = str(item.get("digest") or "").strip()
        if not relative_path or not digest:
            continue
        try:
            candidate = (root / relative_path).resolve(strict=True)
            candidate.relative_to(root.resolve(strict=True))
            if candidate.is_file():
                valid.append({"path": relative_path, "digest": digest, "source": str(item.get("source") or "")})
        except (OSError, ValueError):
            continue
    return valid


def take_cached_image(
    tenant_id: int,
    config: XiurenConfig,
    state: Mapping[str, Any] | None,
    *,
    chooser=None,
) -> tuple[bytes, dict[str, Any]] | None:
    """从预取池随机取一张并重新校验;返回 (图片, 去掉这张后的新状态),池空返回 None。

    调用方在扣费成功后再保存新状态并删除文件(用 discard_cached_file),这样扣费失败不会丢图。
    """

    items = load_cached_files(tenant_id, config, state)
    while items:
        selected = (chooser or random.choice)(items)
        root = _cache_root(tenant_id, config)
        try:
            data = read_owned_bytes(root / selected["path"], max_bytes=config.max_image_bytes)
            normalized, _ = validate_media_bytes(data, declared_type=None, allowed_types=IMAGE_TYPES, max_bytes=config.max_image_bytes)
        except (ResourceBoundaryError, MediaBoundaryError):
            # 这张图坏了或不合规:直接丢掉,换下一张。
            items = [item for item in items if item is not selected]
            discard_cached_file(tenant_id, config, selected)
            continue
        remaining = [item for item in items if item is not selected]
        new_state = {**(dict(state) if isinstance(state, Mapping) else {}), "files": remaining}
        return normalized, {"state": new_state, "taken": selected}
    return None


def discard_cached_file(tenant_id: int, config: XiurenConfig, item: Mapping[str, Any]) -> None:
    """删除已发送(或损坏)的预取文件;文件不存在不算错误。"""

    try:
        (_cache_root(tenant_id, config) / str(item.get("path") or "")).unlink(missing_ok=True)
    except OSError:
        pass


def build_client(config: XiurenConfig) -> S3Client:
    """创建 MinIO 客户端;端点指向本机或链路本地地址时拒绝。"""

    reject_unsafe_endpoint(config.minio_endpoint)
    return S3Client(config.s3_config())


def prefetch_images(client: S3Client, tenant_id: int, config: XiurenConfig, state: Mapping[str, Any] | None, *, sampler=None) -> dict[str, Any]:
    """从 MinIO 随机预读取 prefetch_count 张,和池里剩余的合并后返回新状态。

    不做去重管理:同一张图重复出现没有关系。单张失败只计数,不影响其他张。
    """

    existing = load_cached_files(tenant_id, config, state)
    objects = [
        (key, size)
        for key, size in client.list_objects(config.minio_prefix, max_keys=MAX_LISTED_OBJECTS)
        if key.lower().endswith(IMAGE_SUFFIXES) and 0 < size <= config.max_image_bytes
    ]
    chosen = (sampler or random.sample)(objects, min(config.prefetch_count, len(objects))) if objects else []
    root = _cache_root(tenant_id, config)
    root.mkdir(parents=True, exist_ok=True)
    by_digest = {item["digest"]: item for item in existing}
    downloaded = failed = 0
    for key, _size in chosen:
        try:
            raw = client.get_object(key, max_bytes=config.max_image_bytes)
            content, _ = validate_media_bytes(raw, declared_type=None, allowed_types=IMAGE_TYPES, max_bytes=config.max_image_bytes)
            digest = hashlib.sha256(content).hexdigest()
            (root / f"{digest}.img").write_bytes(content)
            by_digest[digest] = {"path": f"{digest}.img", "digest": digest, "source": key}
            downloaded += 1
        except (S3Error, OSError, MediaBoundaryError, ValueError):
            failed += 1
    # 池子上限是预读张数的两倍,避免没人用时无限增长。
    items = list(by_digest.values())
    limit = config.prefetch_count * 2
    files, dropped = items[-limit:], items[:-limit]
    for item in dropped:
        discard_cached_file(tenant_id, config, item)
    return {"files": files, "downloaded": downloaded, "failed": failed, "listed": len(objects)}


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
