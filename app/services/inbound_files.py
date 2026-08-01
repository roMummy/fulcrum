from __future__ import annotations

import json
import secrets
from datetime import datetime

from app.core.time import now_shanghai
from pathlib import Path
from typing import Any

from fastapi import Request

from app.core.settings import settings
from app.services.outbound_files import build_public_base_url, sanitize_upload_filename


# 入站文件统一落到服务端自己的数据目录下，避免依赖 provider 客户端机器本地磁盘。
INBOUND_FILE_ROOT = Path(settings.data_dir) / "tenants"
INBOUND_FILE_ROOT.mkdir(parents=True, exist_ok=True)


def store_inbound_upload(
    *,
    tenant_id: int,
    channel_id: int,
    file_name: str,
    content: bytes,
    content_type: str | None,
    message_type: str,
    source_event_key: str | None = None,
) -> dict[str, Any]:
    # 这里和 outbound_files 保持同样的分层方式：channel / share_id / 文件名。
    # 这样后续如果要做清理、归档或审计，可以直接按目录定位，不需要再翻数据库。
    normalized_type = str(message_type or "file").strip().lower() or "file"
    original_name = sanitize_upload_filename(file_name, fallback="upload.bin")
    share_id = secrets.token_hex(16)
    if int(tenant_id or 0) <= 0:
        raise ValueError("tenant_id_required")
    target_dir = INBOUND_FILE_ROOT / str(tenant_id) / "inbound_files" / str(channel_id) / share_id
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
        "source_event_key": str(source_event_key or "").strip() or None,
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


def build_inbound_file_download_url(request: Request, *, channel_id: int, share_id: str, file_name: str) -> str:
    public_base = build_public_base_url(request)
    return f"{public_base}/inbound-files/{channel_id}/{share_id}/{file_name}"
