"""开放媒体上传和受控引用接口。"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from app.api.open_api import (
    _authenticate_request,
    _guard_request,
    _load_file_bytes_from_payload,
    _load_authenticated_app,
    _parse_json_body,
)
from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.services.open_api_quotas import finalize_open_api_usage
from app.services.outbound_files import build_public_base_url, store_outbound_upload


router = APIRouter(tags=["open-api-media"])


@router.post("/api/open/media")
async def open_media_upload_api(request: Request):
    """接收业务系统媒体并返回短引用信息，消息发送再引用该 URL。"""

    raw_body = await request.body()
    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, raw_body)
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        payload = _parse_json_body(raw_body)
        channel_id = int(payload.get("channel_id") or 0)
        if channel_id <= 0:
            raise HTTPException(status_code=400, detail="channel_id_required")
        if str(payload.get("file_path") or "").strip():
            # 公共开放接口不能读取网关服务器任意本地路径；业务系统应提交 base64
            # 或先把文件放到可访问 URL，再由消息接口引用。
            raise HTTPException(status_code=400, detail="file_path_not_allowed")
        content = _load_file_bytes_from_payload(payload)
        if not content:
            raise HTTPException(status_code=400, detail="media_content_required")
        file_name = str(payload.get("file_name") or payload.get("name") or "upload.bin").strip() or "upload.bin"
        guard = _guard_request(
            db,
            app=app,
            request_id=str(payload.get("request_id") or "").strip(),
            usage={"api_requests": 1, "media_bytes": len(content)},
            channel_id=channel_id,
        )
        stored = store_outbound_upload(
            tenant_id=credential.tenant_id,
            channel_id=channel_id,
            file_name=file_name,
            content=content,
            content_type=str(payload.get("content_type") or "application/octet-stream"),
            message_type=str(payload.get("media_type") or "file"),
        )
        result = {
            "ok": True,
            "media_id": stored["share_id"],
            "file_name": stored["file_name"],
            "file_size": stored["file_size"],
            "content_type": stored["content_type"],
            "url": f"{build_public_base_url(request)}/outbound-files/{channel_id}/{stored['share_id']}/{quote(stored['file_name'])}",
        }
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result


@router.get("/api/open/media/{media_id}")
async def open_media_detail_api(media_id: str, request: Request, channel_id: int, file_name: str):
    """返回已授权媒体的受控下载入口。"""

    with platform_session(SessionLocal) as auth_db:
        credential = _authenticate_request(auth_db, request, b"")
    with tenant_scope(credential.tenant_id), tenant_session(TenantSessionLocal) as db:
        app = _load_authenticated_app(db, credential)
        guard = _guard_request(
            db,
            app=app,
            request_id=request.headers.get("X-Request-ID"),
            usage={"api_requests": 1},
            channel_id=channel_id,
        )
        # 存储层以 share_id 作为不可猜测引用；这里只返回统一入口，真实文件名由
        # 上传响应交给调用方保存，平台不会暴露服务器绝对路径。
        result = {
            "ok": True,
            "media_id": str(media_id),
            "download_url": f"{build_public_base_url(request)}/outbound-files/{channel_id}/{media_id}/{quote(file_name)}",
        }
        finalize_open_api_usage(db, reservation=guard.reservation, status="committed")
        db.commit()
        return result

