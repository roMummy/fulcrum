from __future__ import annotations

import json
import secrets
from datetime import datetime

from app.core.time import now_shanghai
from pathlib import Path
from typing import Any

from fastapi import Request

from app.core.settings import settings


# 所有租户文件统一放在 data/tenants 下，避免不同客户即使渠道编号或随机分享编号
# 相同也落入同一个目录树。OUTBOUND_FILE_ROOT 保留为稳定的测试和运维定位入口。
OUTBOUND_FILE_ROOT = Path(settings.data_dir) / "tenants"
OUTBOUND_FILE_ROOT.mkdir(parents=True, exist_ok=True)


def sanitize_upload_filename(value: str | None, *, fallback: str = "upload.bin") -> str:
    normalized = str(value or "").strip()
    invalid_chars = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid_chars else char for char in normalized)
    cleaned = cleaned.replace("..", "_").strip(" .")
    return cleaned or fallback


def build_public_base_url(request: Request) -> str:
    # 文件链接会被 Windows provider 独立下载，不能依赖管理员浏览器当前访问地址；
    # 生产/局域网部署应配置 PUBLIC_BASE_URL，让 provider 始终拿到自己可访问的 MBA 地址。
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
