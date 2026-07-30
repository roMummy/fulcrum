from __future__ import annotations

import base64
import binascii
import hmac
import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.core.db import SessionLocal
from app.models.entities import Channel
from app.services.crud import get_channel
from app.services.inbound_files import INBOUND_FILE_ROOT, build_inbound_file_download_url, store_inbound_upload


router = APIRouter(tags=["channel-inbound"])


def _load_provider_token(channel: Channel) -> str:
    # provider token 存在于渠道的 adapter_config_json 里，和现有 workwechat provider 的 header 配置一致。
    raw = str(channel.adapter_config_json or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    headers = parsed.get("headers") if isinstance(parsed.get("headers"), dict) else {}
    token = str(headers.get("X-Provider-Token") or headers.get("x-provider-token") or parsed.get("provider_token") or "").strip()
    return token


def _assert_provider_upload_authorized(channel: Channel, request: Request) -> None:
    expected = _load_provider_token(channel)
    if not expected:
        raise HTTPException(status_code=503, detail="provider_token_not_configured")
    provided = str(request.headers.get("X-Provider-Token") or "").strip()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid_provider_token")


@router.post("/webhooks/{channel_id}/inbound-upload")
async def inbound_file_upload(channel_id: int, request: Request):
    db = SessionLocal()
    try:
        channel = get_channel(db, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        _assert_provider_upload_authorized(channel, request)

        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be object")

        file_name = str(body.get("file_name") or body.get("name") or "upload.bin").strip() or "upload.bin"
        content_base64 = str(body.get("content_base64") or "").strip()
        if not content_base64:
            raise HTTPException(status_code=400, detail="content_base64 is required")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid_content_base64: {exc}") from exc
        if not content:
            raise HTTPException(status_code=400, detail="empty file")

        stored = store_inbound_upload(
            channel_id=channel.id,
            file_name=file_name,
            content=content,
            content_type=str(body.get("content_type") or "").strip() or None,
            message_type=str(body.get("message_type") or "file").strip() or "file",
            source_event_key=str(body.get("source_event_key") or body.get("event_key") or "").strip() or None,
        )
        download_url = build_inbound_file_download_url(
            request,
            channel_id=channel.id,
            share_id=stored["share_id"],
            file_name=quote(stored["file_name"]),
        )
        return {
            "ok": True,
            "channel_id": channel.id,
            "message_type": body.get("message_type") or "file",
            "file_name": stored["file_name"],
            "file_size": stored["file_size"],
            "content_type": stored["content_type"],
            "share_id": stored["share_id"],
            "storage_path": str(stored["target_path"]),
            "download_url": download_url,
        }
    finally:
        db.close()


@router.get("/inbound-files/{channel_id}/{share_id}/{file_name}")
async def inbound_file_download(channel_id: int, share_id: str, file_name: str):
    # 这个下载入口主要用于排障和后续插件处理时的人工回看，文件本身已经在主服务落盘。
    safe_name = str(file_name or "").strip()
    if not safe_name:
        raise HTTPException(status_code=404, detail="file not found")
    target_path = INBOUND_FILE_ROOT / str(channel_id) / share_id / safe_name
    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path=target_path, filename=safe_name)
