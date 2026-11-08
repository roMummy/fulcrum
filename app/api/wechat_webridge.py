from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from sqlalchemy import select

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_db import platform_session, tenant_session
from app.core.tenant_context import tenant_scope
from app.models.entities import Channel
from app.services.channel_events import ingest_inbound_event
from app.services.saas_auth import has_tenant_role
from app.services.wechat_webridge.service import (
    accept_command_result,
    accept_state,
    claim_commands,
    mark_event_seen,
    register_device,
    enqueue_command,
    get_command_result,
    command_catalog,
    list_command_results,
)
from app.models.entities import MessageAttachment
from app.services.inbound_media import store_uploaded_media_attachment

router = APIRouter(prefix="/api/wechat-webridge", tags=["wechat-webridge"])


def _channel_tenant(channel_id: int) -> tuple[int, int]:
    """只通过平台会话解析渠道租户，避免请求正文决定租户归属。"""

    with platform_session(SessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
        return int(channel.tenant_id), int(channel.id)


def _load_channel(channel_id: int):
    tenant_id, actual_id = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, actual_id)
        if channel is None or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
        return tenant_id, channel


async def _json(request: Request) -> dict:
    """严格要求 JSON 对象，给插件返回可重试的 4xx。"""

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request_body_must_be_object")
    return body


@router.get("/{channel_id}/capabilities")
async def capabilities(channel_id: int, request: Request):
    """返回可直接用于控制台和集成方的微桥命令目录。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        return {"ok": True, "channel_id": channel_id, **command_catalog(channel)}


@router.post("/{channel_id}/agent/register")
async def register(channel_id: int, request: Request):
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        try:
            return register_device(db, channel, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{channel_id}/agent/state")
async def state(channel_id: int, request: Request):
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        try:
            return accept_state(db, db.get(Channel, int(channel_id)), body)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/agent/event")
async def event(channel_id: int, request: Request):
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if not channel:
            raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
        try:
            # 事件 ID 先写回顶层，适配器和统一事件服务都能使用同一个去重键。
            body.setdefault("event_id", mark_event_seen(channel, body))
            result = ingest_inbound_event(db, channel, body, headers=dict(request.headers), source="wechat_webridge")
            db.commit()
            return {"ok": True, "deduplicated": bool(result.get("deduplicated")), "event_id": body["event_id"]}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/agent/poll")
async def poll(channel_id: int, request: Request):
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        try:
            commands = claim_commands(db, db.get(Channel, int(channel_id)), body)
            return {"commands": commands}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/agent/command_result")
async def command_result(channel_id: int, request: Request):
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        try:
            return accept_command_result(db, db.get(Channel, int(channel_id)), body)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/agent/upload_media")
async def upload_media(channel_id: int, request: Request, attachment_id: int, file: UploadFile = File(...)):
    """接收手机执行 upload_message_media 后回传的媒体文件。"""

    body = {
        "device_id": request.headers.get("x-webridge-device-id", ""),
        "session_id": request.headers.get("x-webridge-session-id", ""),
        "account_id": request.headers.get("x-webridge-account-id", ""),
    }
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        try:
            from app.services.wechat_webridge.service import _identity_matches

            if not _identity_matches(channel, body):
                raise ValueError("webridge_identity_mismatch")
            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.id == int(attachment_id), MessageAttachment.channel_id == channel.id))
            if attachment is None:
                raise ValueError("webridge_attachment_not_found")
            result = store_uploaded_media_attachment(
                db,
                attachment=attachment,
                content=await file.read(),
                filename=file.filename or "",
                content_type=file.content_type or "application/octet-stream",
            )
            db.commit()
            return {"ok": True, **result}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/commands")
async def enqueue_generic_command(channel_id: int, request: Request):
    """供已登录租户操作员提交 WeBridge 全量命令；手机侧仍只通过 poll 收取。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        try:
            job = enqueue_command(
                db,
                channel,
                command_name=str(body.get("command_name") or body.get("type") or ""),
                args=body.get("args") if isinstance(body.get("args"), dict) else {},
                request_id=str(body.get("request_id") or ""),
            )
            db.commit()
            payload = __import__("app.services.task_queue", fromlist=["parse_job_payload"]).parse_job_payload(job)
            return {"ok": True, "job_id": int(job.id), "request_id": payload.get("command", {}).get("request_id"), "status": job.status}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{channel_id}/commands/{request_id}")
async def command_result_query(channel_id: int, request_id: str, request: Request):
    """查询通用命令的排队、执行或失败结果。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        result = get_command_result(db, db.get(Channel, int(channel_id)), request_id)
        if result is None:
            raise HTTPException(status_code=404, detail="webridge_command_not_found")
        return {"ok": True, **result}


@router.get("/{channel_id}/commands")
async def command_results(channel_id: int, request: Request, status: str = "", command_type: str = "", limit: int = 50):
    """查询当前渠道最近的微桥命令执行记录。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        return {"ok": True, "channel_id": channel_id, "items": list_command_results(db, channel, status=status, command_type=command_type, limit=limit)}
