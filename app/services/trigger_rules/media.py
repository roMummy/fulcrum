"""触发规则的素材存储:图片、语音、视频上传到租户自己的目录,规则里只保存服务端生成的文件名。

安全:文件名由服务端生成(随机串 + 白名单扩展名),规则里的文件名读取前再按固定格式校验并确认在租户目录内,
所以不会出现路径穿越或读到别的租户的文件。图片和视频额外检查文件头,避免把别的东西改个扩展名传上来。
"""

from __future__ import annotations

import base64
import re
import uuid
from pathlib import Path

from app.core.settings import settings

# 素材类别 → (允许的扩展名, 体积上限字节)
MEDIA_KINDS: dict[str, tuple[frozenset[str], int]] = {
    "image": (frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp"}), 5 * 1024 * 1024),
    "audio": (frozenset({".mp3", ".wav", ".amr", ".silk", ".m4a"}), 5 * 1024 * 1024),
    "video": (frozenset({".mp4"}), 20 * 1024 * 1024),
}
_STORED_NAME = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,5}$")

# 没上传封面时使用的 16x16 灰色 JPEG(微桥会从视频自动生成缩略图,其他渠道需要一张封面)
PLACEHOLDER_COVER_BASE64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABQODxIPDRQSEBIXFRQYHjIhHhwcHj0sLiQySUBMS0dARkVQWnNiUFVtVkVGZIhlbXd7gYKBTmCNl4x9lnN+gXz/2wBDARUXFx4aHjshITt8U0ZTfHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHx8fHz/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDdooooA//Z"
)


class MediaError(ValueError):
    """素材不合法;消息可直接展示给用户。"""


def media_root(tenant_id: int) -> Path:
    """租户专属的素材目录;不同租户互不可见。"""

    return (Path(settings.data_dir) / "tenants" / str(int(tenant_id)) / "trigger_media").resolve()


def _looks_right(kind: str, extension: str, data: bytes) -> bool:
    if kind == "image":
        return (
            data[:3] == b"\xff\xd8\xff"
            or data[:8] == b"\x89PNG\r\n\x1a\n"
            or data[:4] == b"GIF8"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        )
    if kind == "video":
        return data[4:8] == b"ftyp"
    return True  # 音频格式多样,以扩展名白名单和体积上限为准


def save_upload(tenant_id: int, kind: str, original_name: str, data: bytes) -> str:
    """保存上传的素材,返回服务端生成的文件名。"""

    if kind not in MEDIA_KINDS:
        raise MediaError("不支持的素材类型")
    extensions, limit = MEDIA_KINDS[kind]
    extension = Path(str(original_name or "")).suffix.lower()
    label = {"image": "图片", "audio": "语音", "video": "视频"}[kind]
    if extension not in extensions:
        raise MediaError(f"{label}格式不支持,请上传 {'、'.join(sorted(item.lstrip('.') for item in extensions))}")
    if not data:
        raise MediaError(f"{label}文件是空的")
    if len(data) > limit:
        raise MediaError(f"{label}不能超过 {limit // 1024 // 1024}MB")
    if not _looks_right(kind, extension, data):
        raise MediaError(f"文件内容不是有效的{label}")
    root = media_root(tenant_id)
    root.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{extension}"
    (root / stored).write_bytes(data)
    return stored


def resolve(tenant_id: int, stored_name: str, kind: str) -> Path:
    """把规则里保存的文件名变成租户目录内的真实路径;格式不对、类型不符、文件不存在都拒绝。"""

    name = str(stored_name or "").strip()
    if not _STORED_NAME.match(name) or kind not in MEDIA_KINDS or Path(name).suffix not in MEDIA_KINDS[kind][0]:
        raise MediaError("素材文件无效,请重新上传")
    root = media_root(tenant_id)
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise MediaError("素材文件已丢失,请重新上传")
    return path


def read_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def delete(tenant_id: int, stored_name: str) -> None:
    """删除一个素材;文件名格式不对时忽略(不会误删任意文件)。"""

    name = str(stored_name or "").strip()
    if _STORED_NAME.match(name):
        (media_root(tenant_id) / name).unlink(missing_ok=True)
