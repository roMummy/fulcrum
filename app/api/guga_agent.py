"""咕嘎助手手机端主动连接平台的 Agent 接入 API。"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.concurrency import run_in_threadpool

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.logging import get_logger
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Channel
from app.services.account_connections import verify_account_connection
from app.services.account_connections.contracts import AccountConnectionError
from app.services.guga_agent import service
from app.services.guga_agent import ws as agent_ws
from app.services.guga_agent.access import account_info, verify_connection_token
from app.services.guga_agent.contract import GugaAgentError

router = APIRouter(prefix="/api/guga-agent", tags=["guga-agent"])
logger = get_logger(__name__)

POLL_HOLD_SECONDS = 20
POLL_CHECK_INTERVAL_SECONDS = 1


def _payload_summary(payload: dict) -> dict[str, object]:
    summary: dict[str, object] = {"keys": sorted(str(key) for key in payload.keys())}
    for key in ("request_id", "command_name", "event_id", "event_name", "device_id", "account_id"):
        value = payload.get(key)
        if value not in (None, ""):
            text = str(value)
            summary[key] = text[:120] if key not in {"device_id", "account_id"} else f"{text[:8]}..."
    return summary


def _channel_tenant(channel_id: int) -> tuple[int, int]:
    """只按渠道主键解析租户,不信任请求正文里的租户信息。"""

    with platform_session(SessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None or channel.channel_type != "wechat_gugatool" or channel.deleted_at is not None:
            raise HTTPException(status_code=404, detail="guga_agent_channel_not_found")
        return int(channel.tenant_id), int(channel.id)


def _require_device_token(channel_id: int, connection_token: str) -> None:
    _channel_tenant(channel_id)
    with platform_session(SessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None or not verify_connection_token(channel, connection_token):
            raise HTTPException(status_code=403, detail="guga_agent_connection_token_invalid")


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="request_body_must_be_json") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request_body_must_be_object")
    return body


def _error_response(exc: GugaAgentError, *, channel_id: int) -> dict:
    logger.warning("guga agent request rejected", extra={"channel_id": channel_id, "error_code": exc.code})
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.status,
        content={"ok": False, "message": exc.message, "error": {"code": exc.code, "message": exc.message}},
    )


@router.post("/{channel_id}/{connection_token}/agent/register", dependencies=[Depends(_require_device_token)])
async def register(channel_id: int, request: Request):
    body = await _json(request)
    logger.info("guga agent register received", extra={"channel_id": channel_id, "payload": _payload_summary(body)})
    tenant_id, _ = _channel_tenant(channel_id)
    try:
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            channel = db.get(Channel, int(channel_id))
            if channel is None:
                raise HTTPException(status_code=404, detail="guga_agent_channel_not_found")
            data = service.register_device(db, channel, body)
            if channel.is_enabled:
                return {"ok": True, "data": {**data, "connection_state": "active", "message": "微信账号已连接"}}
            verified = verify_account_connection(db, channel_id=int(channel.id))
            return {
                "ok": True,
                "data": {
                    **data,
                    "connection_state": verified.state,
                    "message": "请在网页核对微信账号信息并确认接入",
                },
            }
    except GugaAgentError as exc:
        return _error_response(exc, channel_id=channel_id)
    except AccountConnectionError as exc:
        logger.exception("guga agent register verify failed", extra={"channel_id": channel_id, "error_code": str(exc)})
        return {"ok": True, "data": {"connection_state": "pending", "message": str(exc)}}


@router.post("/{channel_id}/{connection_token}/agent/state", dependencies=[Depends(_require_device_token)])
async def state(channel_id: int, request: Request):
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    try:
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            channel = db.get(Channel, int(channel_id))
            if channel is None:
                raise HTTPException(status_code=404, detail="guga_agent_channel_not_found")
            return service.accept_state(db, channel, body, transport="http")
    except GugaAgentError as exc:
        return _error_response(exc, channel_id=channel_id)


@router.get("/{channel_id}/{connection_token}/agent/health", dependencies=[Depends(_require_device_token)])
async def health(channel_id: int):
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None:
            raise HTTPException(status_code=404, detail="guga_agent_channel_not_found")
        return {"ok": True, "data": account_info(channel)}


@router.post("/{channel_id}/{connection_token}/agent/command_result", dependencies=[Depends(_require_device_token)])
async def command_result(channel_id: int, request: Request):
    body = await _json(request)
    logger.info("guga agent command result received", extra={"channel_id": channel_id, "payload": _payload_summary(body)})
    tenant_id, _ = _channel_tenant(channel_id)
    try:
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            channel = db.get(Channel, int(channel_id))
            if channel is None:
                raise HTTPException(status_code=404, detail="guga_agent_channel_not_found")
            return service.accept_command_result(db, channel, body)
    except GugaAgentError as exc:
        return _error_response(exc, channel_id=channel_id)


@router.post("/{channel_id}/{connection_token}/agent/event", dependencies=[Depends(_require_device_token)])
async def event(channel_id: int, request: Request):
    body = await _json(request)
    logger.info("guga agent event received", extra={"channel_id": channel_id, "payload": _payload_summary(body)})
    tenant_id, _ = _channel_tenant(channel_id)
    try:
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            channel = db.get(Channel, int(channel_id))
            if channel is None:
                raise HTTPException(status_code=404, detail="guga_agent_channel_not_found")
            return service.ingest_event(db, channel, body)
    except GugaAgentError as exc:
        return _error_response(exc, channel_id=channel_id)


def _claim_once(channel_id: int, tenant_id: int, body: dict) -> list[dict]:
    """独立短会话领取命令,挂起期间不持有数据库连接。"""

    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None:
            return []
        return service.claim_commands(db, channel, body, limit=5)


@router.post("/{channel_id}/{connection_token}/agent/poll", dependencies=[Depends(_require_device_token)])
async def poll(channel_id: int, request: Request):
    """长轮询:有命令立即返回,没有则最多挂起 wait_seconds。"""

    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    try:
        wait_seconds = min(max(float(body.get("wait_seconds") or POLL_HOLD_SECONDS), 1.0), 25.0)
    except (TypeError, ValueError):
        wait_seconds = POLL_HOLD_SECONDS
    deadline = time.monotonic() + wait_seconds
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()
    from app.services.wechat_webridge import wakeup

    wakeup.register(channel_id, loop, wake)
    try:
        while True:
            wake.clear()
            try:
                commands = await run_in_threadpool(_claim_once, channel_id, tenant_id, body)
            except GugaAgentError as exc:
                return _error_response(exc, channel_id=channel_id)
            if commands or time.monotonic() >= deadline or await request.is_disconnected():
                return {"ok": True, "data": {"commands": commands, "next_poll_after_ms": 0}}
            try:
                await asyncio.wait_for(wake.wait(), timeout=POLL_CHECK_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        wakeup.unregister(channel_id, loop, wake)


@router.websocket("/{channel_id}/{connection_token}/agent/ws")
async def websocket_agent(websocket: WebSocket, channel_id: int, connection_token: str):
    """手机主动建立的 WebSocket 命令通道;事件仍走 HTTP。"""

    try:
        _channel_tenant(channel_id)
        with platform_session(SessionLocal) as db:
            channel = db.get(Channel, int(channel_id))
            valid = channel is not None and verify_connection_token(channel, connection_token)
        if not valid:
            await websocket.close(code=1008, reason="invalid_token")
            return
        tenant_id, _ = _channel_tenant(channel_id)
        await agent_ws.serve(websocket, channel_id=channel_id, tenant_id=tenant_id)
    except HTTPException:
        await websocket.close(code=1008, reason="channel_not_found")
