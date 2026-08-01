from __future__ import annotations

import base64
import binascii
import hmac
import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Channel
from app.services.inbound_files import INBOUND_FILE_ROOT, build_inbound_file_download_url, store_inbound_upload
from app.services.outbound_files import resolve_tenant_share_file


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
    # Provider 令牌先在非租户会话中解析成最小渠道身份，认证成功后才允许建立
    # tenant_scope。这样 URL 主键和 JSON 正文都不能自行决定租户归属。
    with platform_session(SessionLocal) as auth_db:
        channel = auth_db.get(Channel, channel_id)
        if not channel:
            raise HTTPException(status_code=404, detail="Channel not found")
        _assert_provider_upload_authorized(channel, request)
        credential = (int(channel.tenant_id), int(channel.id))

    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, credential[1])
        if channel is None:
            raise HTTPException(status_code=404, detail="Channel not found")
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
            tenant_id=channel.tenant_id,
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


@router.get("/inbound-files/{channel_id}/{share_id}/{file_name}")
async def inbound_file_download(channel_id: int, share_id: str, file_name: str):
    # 这个下载入口主要用于排障和后续插件处理时的人工回看，文件本身已经在主服务落盘。
    with platform_session(SessionLocal) as db:
        channel = db.get(Channel, channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="file not found")
        tenant_id = int(channel.tenant_id)
    target_path = resolve_tenant_share_file(
        root=INBOUND_FILE_ROOT,
        tenant_id=tenant_id,
        namespace="inbound_files",
        channel_id=channel_id,
        share_id=share_id,
        file_name=file_name,
    )
    if target_path is None:
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(path=target_path, filename=target_path.name)
