from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_db import platform_session, tenant_session
from app.core.tenant_context import tenant_scope
from app.models.entities import Channel
from app.services.channel_events import ingest_inbound_event
from app.services.wechat_webridge.service import (
    accept_command_result,
    accept_state,
    claim_commands,
    mark_event_seen,
    register_device,
)

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
