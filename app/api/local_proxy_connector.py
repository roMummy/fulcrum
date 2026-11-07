"""Windows 本地代理连接器的激活、心跳和出站 WebSocket 接口。"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.core.db import SessionLocal
from app.core.tenant_context import tenant_scope
from app.services.local_proxy_connector import (
    LocalProxyConnectorError,
    authenticate_device,
    consume_activation_code,
    mark_connector_heartbeat,
)
from app.services.local_proxy_connector_ws import (
    LocalConnectorSocket,
    local_proxy_connector_ws_registry,
)
from app.services.login_proxy_pool import fail_login_proxy_session_by_key


router = APIRouter(prefix="/api/local-connector", tags=["local-proxy-connector"])


async def _read_json_object(request: Request) -> dict[str, object]:
    """只接受 JSON 对象，避免把任意请求体原样写入日志或错误响应。"""

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="json_object_required")
    return body


@router.post("/activate")
async def activate_local_proxy_connector(request: Request) -> dict[str, object]:
    """使用租户管理员生成的一次性激活码绑定 Windows 设备。"""

    body = await _read_json_object(request)
    db = SessionLocal()
    try:
        result = consume_activation_code(
            db,
            code=str(body.get("activation_code") or ""),
            device_id=str(body.get("device_id") or body.get("fingerprint") or ""),
            client_version=str(body.get("client_version") or ""),
            local_proxy_label=str(body.get("local_proxy_label") or ""),
        )
    except LocalProxyConnectorError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        # consume_activation_code 内部提交事务；这里单独创建的会话必须总是释放。
        db.close()
    base_url = str(request.base_url).rstrip("/")
    return {
        "ok": True,
        "connector_id": result["connector_id"],
        "device_key": result["device_key"],
        "heartbeat_url": f"{base_url}/api/local-connector/heartbeat",
        "websocket_url": f"{base_url.replace('http://', 'ws://').replace('https://', 'wss://')}/api/local-connector/ws",
    }


@router.post("/heartbeat")
async def local_proxy_connector_heartbeat(request: Request) -> dict[str, object]:
    """设备密钥认证后刷新在线状态，不接受客户端指定租户归属。"""

    body = await _read_json_object(request)
    device_key = str(request.headers.get("X-Local-Connector-Key") or body.get("device_key") or "").strip()
    db = SessionLocal()
    try:
        connector = authenticate_device(
            db,
            device_key=device_key,
            client_version=str(body.get("client_version") or ""),
            local_proxy_label=str(body.get("local_proxy_label") or ""),
        )
        with tenant_scope(int(connector.tenant_id)):
            mark_connector_heartbeat(db, connector=connector)
        return {"ok": True, "connector_id": int(connector.id)}
    except LocalProxyConnectorError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    finally:
        db.close()


@router.websocket("/ws")
async def local_proxy_connector_websocket(websocket: WebSocket) -> None:
    """连接器主动建立的出站会话，控制帧只处理设备心跳和会话状态。"""

    device_key = str(websocket.query_params.get("device_key") or "").strip()
    if not device_key:
        await websocket.close(code=1008)
        return
    db = SessionLocal()
    connector = None
    try:
        connector = authenticate_device(db, device_key=device_key)
    except LocalProxyConnectorError:
        db.close()
        await websocket.close(code=1008)
        return
    await websocket.accept()
    connection = LocalConnectorSocket(
        connector_id=int(connector.id),
        tenant_id=int(connector.tenant_id),
        websocket=websocket,
    )
    await local_proxy_connector_ws_registry.register(connection)
    try:
        await websocket.send_text(
            json.dumps(
                {"type": "hello_ack", "connector_id": int(connector.id), "tenant_id": int(connector.tenant_id)},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw_bytes = message.get("bytes")
            if raw_bytes:
                # 连接器回传的二进制帧格式为 tunnel_id + 0 分隔符 + TCP 数据。
                tunnel_id_bytes, separator, data = bytes(raw_bytes).partition(b"\x00")
                if separator:
                    from app.services.local_proxy_connector_ws import local_proxy_connector_ws_registry

                    await local_proxy_connector_ws_registry.send_tunnel_data(
                        tunnel_id_bytes.decode("utf-8", errors="ignore"),
                        data,
                    )
                continue
            raw_text = message.get("text")
            if not raw_text:
                continue
            try:
                payload = json.loads(raw_text)
            except (TypeError, ValueError):
                await websocket.send_text(json.dumps({"type": "error", "code": "invalid_control_message"}))
                continue
            if not isinstance(payload, dict):
                continue
            message_type = str(payload.get("type") or "").strip()
            if message_type == "heartbeat":
                with tenant_scope(int(connector.tenant_id)):
                    mark_connector_heartbeat(db, connector=connector)
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}, separators=(",", ":")))
            elif message_type == "session_bind":
                try:
                    await local_proxy_connector_ws_registry.bind_session(
                        int(connector.id), str(payload.get("session_key") or "")
                    )
                    await websocket.send_text(json.dumps({"type": "session_bind_ack"}, separators=(",", ":")))
                except ValueError as exc:
                    await websocket.send_text(json.dumps({"type": "error", "code": str(exc)}))
            elif message_type == "session_unbind":
                await local_proxy_connector_ws_registry.unbind_session(str(payload.get("session_key") or ""))
            elif message_type == "open_result":
                await local_proxy_connector_ws_registry.resolve_open_result(
                    str(payload.get("tunnel_id") or ""),
                    ok=bool(payload.get("ok")),
                    error=str(payload.get("error") or ""),
                )
            elif message_type == "data":
                try:
                    data = bytes.fromhex(str(payload.get("data") or ""))
                except ValueError:
                    data = b""
                if data:
                    await local_proxy_connector_ws_registry.send_tunnel_data(
                        str(payload.get("tunnel_id") or ""),
                        data,
                    )
    except WebSocketDisconnect:
        pass
    finally:
        sessions = await local_proxy_connector_ws_registry.unregister(int(connector.id), websocket)
        db.close()
        # 连接器断开必须立即终止对应扫码会话并释放临时凭证，避免 874 继续使用半开的代理。
        for session_key in sessions:
            cleanup_db = SessionLocal()
            try:
                with tenant_scope(int(connector.tenant_id)):
                    fail_login_proxy_session_by_key(
                        cleanup_db,
                        session_key=session_key,
                        tenant_id=int(connector.tenant_id),
                    )
            finally:
                cleanup_db.close()
