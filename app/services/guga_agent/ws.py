"""咕嘎助手 Agent WebSocket 服务端。

手机在 NAT 后主动连入,首帧 hello 完成设备注册;之后服务端下发命令,
手机回报 command_result/state/ping。事件仍以 HTTP /agent/event 上报,
与 GugaTool 客户端实现保持一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from app.core.db import TenantSessionLocal
from app.core.logging import get_logger
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel
from app.services.account_connections import verify_account_connection

from . import service
from .contract import WS_MAX_COMMAND_BYTES, as_object, text

logger = get_logger(__name__)

_connections: dict[int, WebSocket] = {}
_connections_lock = asyncio.Lock()

HELLO_TIMEOUT_SECONDS = 10
COMMAND_POLL_INTERVAL_SECONDS = 0.5


async def serve(websocket: WebSocket, *, channel_id: int, tenant_id: int) -> None:
    """处理一条已通过路径 Token 校验的 WebSocket 连接。"""

    await websocket.accept()
    try:
        frame = await asyncio.wait_for(websocket.receive_json(), timeout=HELLO_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, ValueError):
        await _safe_close(websocket, code=4008, reason="hello_timeout")
        return
    hello = as_object(frame)
    if text(hello.get("type")) != "hello":
        await _safe_send(websocket, {"type": "hello_error", "code": "hello_required", "message": "首帧必须是 hello"})
        await _safe_close(websocket, code=4008, reason="hello_required")
        return

    try:
        registration = await run_in_threadpool(_register_from_hello, tenant_id, channel_id, hello)
    except Exception as exc:  # noqa: BLE001
        logger.warning("guga agent websocket hello rejected", extra={"channel_id": channel_id, "error": str(exc)})
        await _safe_send(websocket, {"type": "hello_error", "code": getattr(exc, "code", "hello_failed"), "message": str(exc)})
        await _safe_close(websocket, code=4008, reason="hello_failed")
        return

    await _replace_connection(channel_id, websocket)
    await _safe_send(websocket, {"type": "hello_ack", "session_id": registration.get("session_id", "")})
    logger.info("guga agent websocket connected", extra={"channel_id": channel_id, "tenant_id": tenant_id})

    sender = asyncio.create_task(_sender_loop(websocket, channel_id=channel_id, tenant_id=tenant_id, registration=registration))
    try:
        while True:
            frame = await websocket.receive_json()
            await _handle_frame(tenant_id=tenant_id, channel_id=channel_id, frame=as_object(frame), websocket=websocket)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("guga agent websocket frame failed", extra={"channel_id": channel_id})
    finally:
        sender.cancel()
        await _drop_connection(channel_id, websocket)
        logger.info("guga agent websocket disconnected", extra={"channel_id": channel_id})


def _register_from_hello(tenant_id: int, channel_id: int, hello: dict[str, Any]) -> dict[str, Any]:
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None or channel.channel_type != "wechat_gugatool" or channel.deleted_at is not None:
            raise RuntimeError("guga_agent_channel_not_found")
        registration = service.register_device(db, channel, hello)
        if not channel.is_enabled:
            # 与 HTTP register 一致:首次连接只准备身份,启用仍需网页确认。
            verify_account_connection(db, channel_id=int(channel.id))
        return registration


async def _handle_frame(*, tenant_id: int, channel_id: int, frame: dict[str, Any], websocket: WebSocket) -> None:
    frame_type = text(frame.get("type")).lower()
    if frame_type == "ping":
        await _safe_send(websocket, {"type": "pong"})
        return
    if frame_type == "pong":
        return
    if frame_type == "state":
        await run_in_threadpool(_accept_state, tenant_id, channel_id, frame)
        return
    if frame_type == "command_result":
        await run_in_threadpool(_accept_command_result, tenant_id, channel_id, frame)
        return
    if frame_type == "event":
        await run_in_threadpool(_ingest_event, tenant_id, channel_id, frame)
        return


def _accept_state(tenant_id: int, channel_id: int, frame: dict[str, Any]) -> None:
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is not None:
            service.accept_state(db, channel, frame, transport="ws")


def _accept_command_result(tenant_id: int, channel_id: int, frame: dict[str, Any]) -> None:
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is not None:
            service.accept_command_result(db, channel, frame)


def _ingest_event(tenant_id: int, channel_id: int, frame: dict[str, Any]) -> None:
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is not None:
            service.ingest_event(db, channel, frame)


async def _sender_loop(websocket: WebSocket, *, channel_id: int, tenant_id: int, registration: dict[str, Any]) -> None:
    payload = {
        "device_id": registration.get("device_id", ""),
        "account_id": registration.get("account_id", ""),
        "session_id": registration.get("session_id", ""),
    }
    while True:
        await asyncio.sleep(COMMAND_POLL_INTERVAL_SECONDS)
        try:
            commands = await run_in_threadpool(_claim_commands, tenant_id, channel_id, payload)
        except Exception:  # noqa: BLE001
            continue
        for command in commands:
            sent = await _safe_send(
                websocket,
                {
                    "type": "command",
                    "request_id": command.get("request_id", ""),
                    "command_name": command.get("command_name", ""),
                    "params": command.get("params", {}),
                },
            )
            if not sent:
                return


def _claim_commands(tenant_id: int, channel_id: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if channel is None:
            return []
        return service.claim_commands(db, channel, payload, limit=3, max_payload_bytes=WS_MAX_COMMAND_BYTES)


async def _replace_connection(channel_id: int, websocket: WebSocket) -> None:
    async with _connections_lock:
        previous = _connections.get(int(channel_id))
        _connections[int(channel_id)] = websocket
    if previous is not None and previous is not websocket:
        await _safe_close(previous, code=4001, reason="replaced")


async def _drop_connection(channel_id: int, websocket: WebSocket) -> None:
    async with _connections_lock:
        if _connections.get(int(channel_id)) is websocket:
            _connections.pop(int(channel_id), None)


async def _safe_send(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    try:
        await websocket.send_json(payload)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _safe_close(websocket: WebSocket, *, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001
        pass
