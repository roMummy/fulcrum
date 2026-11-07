from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.core.settings import settings


class ResourceBoundaryError(ValueError):
    """插件访问租户资源时违反目录、文件或生命周期边界。"""


def tenant_media_root(tenant_id: int, plugin_code: str) -> Path:
    """返回当前租户和插件专属的媒体根目录，不回退到共享旧目录。"""

    normalized_tenant = _positive_id(tenant_id, "租户编号")
    normalized_plugin = str(plugin_code or "").strip()
    if not normalized_plugin or any(part in normalized_plugin for part in ("/", "\\", "..")):
        raise ResourceBoundaryError("插件代码不合法")
    root = (Path(settings.data_dir).expanduser() / "tenants" / str(normalized_tenant) / "plugins" / normalized_plugin).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_owned_path(root: str | Path, relative_path: str, *, must_exist: bool = True) -> Path:
    """把相对资源名解析到授权根目录内，拒绝绝对路径、穿越和越界符号链接。"""

    resolved_root = Path(root).expanduser().resolve(strict=True)
    normalized = str(relative_path or "").strip()
    candidate_input = Path(normalized)
    if not normalized or candidate_input.is_absolute():
        raise ResourceBoundaryError("资源必须使用授权目录内的相对路径")
    candidate = (resolved_root / candidate_input).resolve(strict=must_exist)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ResourceBoundaryError("资源不属于当前租户授权目录") from exc
    if must_exist and not candidate.is_file():
        raise ResourceBoundaryError("资源文件不存在")
    return candidate


def iter_owned_files(root: str | Path, *, suffixes: set[str] | frozenset[str] = frozenset()) -> tuple[Path, ...]:
    """只返回授权根目录内的普通文件，忽略越界符号链接和不支持的后缀。"""

    resolved_root = Path(root).expanduser().resolve(strict=True)
    if not resolved_root.is_dir():
        raise ResourceBoundaryError("授权资源目录不存在")
    normalized_suffixes = {str(item).lower() for item in suffixes if str(item).strip()}
    files: list[Path] = []
    for candidate in resolved_root.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            owned = candidate.resolve(strict=True).relative_to(resolved_root)
        except (FileNotFoundError, ValueError):
            continue
        if normalized_suffixes and owned.suffix.lower() not in normalized_suffixes:
            continue
        files.append(candidate.resolve(strict=True))
    return tuple(sorted(files, key=lambda item: str(item)))


def read_owned_bytes(path: str | Path, *, max_bytes: int) -> bytes:
    """读取已确认归属的文件，并在读取前限制体积。"""

    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_file():
        raise ResourceBoundaryError("资源文件不存在")
    limit = _positive_limit(max_bytes, "资源大小上限")
    size = candidate.stat().st_size
    if size <= 0 or size > limit:
        raise ResourceBoundaryError("资源为空或超过大小上限")
    return candidate.read_bytes()


def create_tenant_temp_dir(tenant_id: int, plugin_code: str) -> Path:
    """创建当前租户插件的私有临时目录，目录名不携带外部用户输入。"""

    root = tenant_media_root(tenant_id, plugin_code) / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="job-", dir=root))


def safe_remove_tree(path: str | Path) -> None:
    """只删除明确传入的临时目录，删除失败由调用方记录稳定错误。"""

    candidate = Path(path).expanduser()
    if not candidate.exists():
        return
    if candidate.is_symlink() or not candidate.is_dir():
        raise ResourceBoundaryError("临时资源路径不是目录")
    shutil.rmtree(candidate)


def _positive_id(value: int, label: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ResourceBoundaryError(f"{label}必须是正整数") from exc
    if normalized <= 0:
        raise ResourceBoundaryError(f"{label}必须是正整数")
    return normalized


def _positive_limit(value: int, label: str) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ResourceBoundaryError(f"{label}必须是正整数") from exc
    if normalized <= 0:
        raise ResourceBoundaryError(f"{label}必须是正整数")
    return normalized

