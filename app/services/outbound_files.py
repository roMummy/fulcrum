from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime

from app.core.time import now_shanghai
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Request

from app.core.settings import settings


# 所有租户文件统一放在 data/tenants 下，避免不同客户即使渠道编号或随机分享编号
# 相同也落入同一个目录树。OUTBOUND_FILE_ROOT 保留为稳定的测试和运维定位入口。
OUTBOUND_FILE_ROOT = Path(settings.data_dir) / "tenants"
OUTBOUND_FILE_ROOT.mkdir(parents=True, exist_ok=True)
SHARE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def sanitize_upload_filename(value: str | None, *, fallback: str = "upload.bin") -> str:
    normalized = str(value or "").strip()
    invalid_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid_chars else char for char in normalized)
    cleaned = cleaned.replace("..", "_").strip(" .")
    return cleaned or fallback


def resolve_tenant_share_file(
    *,
    root: Path,
    tenant_id: int,
    namespace: str,
    channel_id: int,
    share_id: str,
    file_name: str,
) -> Path | None:
    """解析公开分享文件并强制限制在可信租户和渠道目录内。"""

    if int(tenant_id or 0) <= 0 or int(channel_id or 0) <= 0:
        return None
    normalized_namespace = str(namespace or "").strip()
    if normalized_namespace not in {"outbound_files", "inbound_files"}:
        return None
    normalized_share_id = str(share_id or "").strip().lower()
    if SHARE_ID_PATTERN.fullmatch(normalized_share_id) is None:
        return None
    normalized_file_name = str(file_name or "").strip()
    safe_name = sanitize_upload_filename(normalized_file_name, fallback="")
    # 存储时文件名已经过同一净化函数处理。下载时若输入还会发生变化，说明请求
    # 包含路径符号、连续点或平台不安全字符，必须直接拒绝而不是猜测目标文件。
    if not safe_name or safe_name != normalized_file_name:
        return None

    channel_root = (root / str(tenant_id) / normalized_namespace / str(channel_id)).resolve()
    target_path = (channel_root / normalized_share_id / safe_name).resolve()
    # resolve 后再次验证包含关系，同时阻止目录穿越和分享目录内恶意符号链接。
    if not target_path.is_relative_to(channel_root):
        return None
    return target_path if target_path.is_file() else None


def build_public_base_url(request: Request) -> str:
    # 文件链接会被 Windows provider 独立下载，不能依赖管理员浏览器当前访问地址；
    # 生产/局域网部署应配置 PUBLIC_BASE_URL，让 provider 始终拿到自己可访问的 Fulcrum 地址。
    configured_base_url = str(settings.public_base_url or "").strip().rstrip("/")
    if configured_base_url:
        return configured_base_url

    # 未配置显式基址时，继续兼容反向代理头和 FastAPI 当前请求地址，方便本机开发和测试。
    proto = str(request.headers.get("X-Forwarded-Proto") or request.url.scheme or "http").strip()
    host = str(request.headers.get("X-Forwarded-Host") or request.headers.get("host") or request.url.netloc).strip()
    return f"{proto}://{host}"


def store_outbound_upload(
    *,
    tenant_id: int,
    channel_id: int,
    file_name: str,
    content: bytes,
    content_type: str | None,
    message_type: str,
) -> dict[str, Any]:
    normalized_type = str(message_type or "file").strip().lower() or "file"
    original_name = sanitize_upload_filename(file_name, fallback="upload.bin")
    share_id = secrets.token_hex(16)
    if int(tenant_id or 0) <= 0:
        raise ValueError("tenant_id_required")
    target_dir = OUTBOUND_FILE_ROOT / str(tenant_id) / "outbound_files" / str(channel_id) / share_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / original_name
    target_path.write_bytes(content)

    metadata = {
        "tenant_id": tenant_id,
        "channel_id": channel_id,
        "message_type": normalized_type,
        "file_name": original_name,
        "content_type": str(content_type or "").strip() or None,
        "file_size": len(content),
        "created_at": now_shanghai().isoformat(),
    }
    (target_dir / "meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "share_id": share_id,
        "file_name": original_name,
        "file_size": len(content),
        "content_type": metadata["content_type"],
        "target_path": target_path,
        "metadata": metadata,
    }


# 出站载荷里可能携带 base64 的字段,按消息类型映射到“文件地址”字段。
# 微桥等协议要求图片、视频、语音传路径或 URL,不接受把大文件塞进请求体。
_MEDIA_BASE64_FIELDS = {
    "image": (("base64", "image_base64"), "image_url"),
    "voice": (("base64", "voice_base64", "audio_base64"), "voice_url"),
    "video": (("base64", "video_base64"), "video_url"),
}
# 封面等附属 base64 不再单独投递,微桥会从视频自动生成缩略图。
_AUXILIARY_BASE64_FIELDS = ("cover_base64", "thumb_base64")


def _sniff_extension(content: bytes, message_type: str) -> str:
    """按文件头判断扩展名;手机端和微信据此识别格式。"""

    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"GIF8":
        return ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[4:8] == b"ftyp":
        return ".mp4"
    if content[:3] == b"ID3" or content[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    return {"image": ".jpg", "video": ".mp4", "voice": ".mp3"}.get(message_type, ".bin")


def externalize_media_payload(payload: dict[str, Any], *, tenant_id: int, channel_id: int, public_base_url: str) -> dict[str, Any]:
    """把载荷里的 base64 媒体落成文件,并改成可下载的文件地址。

    返回新的载荷,不修改入参。库、任务队列和日志里因此只会出现文件地址,
    不会出现 base64。没有配置公开地址时无法生成地址,原样返回由调用方决定如何处理。
    """

    message_type = str(payload.get("message_type") or "").strip().lower()
    spec = _MEDIA_BASE64_FIELDS.get(message_type)
    base = str(public_base_url or "").strip().rstrip("/")
    if spec is None or not base:
        return payload
    source_fields, url_field = spec
    encoded = next((str(payload.get(key) or "").strip() for key in source_fields if payload.get(key)), "")
    if not encoded:
        return payload
    try:
        content = base64.b64decode(encoded, validate=False)
    except (ValueError, TypeError):
        return payload
    if not content:
        return payload
    extension = _sniff_extension(content, message_type)
    stored = store_outbound_upload(
        tenant_id=tenant_id,
        channel_id=channel_id,
        file_name=f"{message_type}{extension}",
        content=content,
        content_type=None,
        message_type=message_type,
    )
    result = {key: value for key, value in payload.items() if key not in (*source_fields, *_AUXILIARY_BASE64_FIELDS, "image_base64")}
    result[url_field] = f"{base}/outbound-files/{channel_id}/{stored['share_id']}/{quote(stored['file_name'])}"
    return result


def redact_base64_fields(value: Any) -> Any:
    """递归把任何 *base64 字段换成体积说明。

    插件回复、出站载荷进入调试信息、日志或任务结果之前都要经过这里,
    库里只允许出现文件地址,绝不能出现 base64 内容。
    """

    if isinstance(value, dict):
        return {
            key: (f"[base64 已省略,{len(item)} 字符]" if str(key).lower().endswith("base64") and isinstance(item, str) else redact_base64_fields(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_base64_fields(item) for item in value]
    return value
