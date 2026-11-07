"""Windows 连接器 WebSocket 控制循环和本机 SOCKS5 数据转发。"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from websockets.asyncio.client import connect

from socks5 import open_socks5_connection


class LocalProxyConnectorClient:
    """连接 SaaS 反向隧道，连接器关闭即释放所有本地代理连接。"""

    def __init__(self, *, server: str, device_key: str, socks: dict[str, Any]) -> None:
        self.server = server.rstrip("/")
        self.device_key = device_key
        self.socks = socks
        self._stopped = False
        self._writers: dict[str, Any] = {}

    async def run(self) -> None:
        ws_url = self.server.replace("https://", "wss://").replace("http://", "ws://")
        ws_url += "/api/local-connector/ws?device_key=" + self.device_key
        while not self._stopped:
            try:
                async with connect(ws_url, max_size=8 * 1024 * 1024) as websocket:
                    await websocket.send(json.dumps({"type": "hello", "protocol_version": 1}, separators=(",", ":")))
                    heartbeat = asyncio.create_task(self._heartbeat_loop(websocket))
                    try:
                        async for message in websocket:
                            if isinstance(message, bytes):
                                # 服务器下发的二进制帧由同一 tunnel_id 路由到本地 SOCKS。
                                continue
                            payload = json.loads(message)
                            await self._handle_control(websocket, payload)
                    finally:
                        heartbeat.cancel()
            except Exception:
                if self._stopped:
                    break
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self._stopped = True

    async def _heartbeat_loop(self, websocket: Any) -> None:
        while True:
            await asyncio.sleep(15)
            await websocket.send(json.dumps({"type": "heartbeat"}, separators=(",", ":")))

    async def _handle_control(self, websocket: Any, payload: dict[str, Any]) -> None:
        message_type = str(payload.get("type") or "").strip()
        if message_type == "open":
            tunnel_id = str(payload.get("tunnel_id") or uuid.uuid4().hex)
            try:
                reader, writer = await open_socks5_connection(
                    str(self.socks.get("host") or "127.0.0.1"),
                    int(self.socks.get("port") or 1080),
                    str(payload.get("target_host") or ""),
                    int(payload.get("target_port") or 0),
                    username=str(self.socks.get("username") or ""),
                    password=str(self.socks.get("password") or ""),
                )
            except Exception as exc:
                await websocket.send(json.dumps({"type": "open_result", "tunnel_id": tunnel_id, "ok": False, "error": str(exc)[:80]}))
                return
            await websocket.send(json.dumps({"type": "open_result", "tunnel_id": tunnel_id, "ok": True}, separators=(",", ":")))
            self._writers[tunnel_id] = writer
            asyncio.create_task(self._pipe_local_to_websocket(websocket, reader, writer, tunnel_id))
        elif message_type == "session_end":
            # 会话结束后不保留连接器上的长期代理状态；服务端会在关闭 WS 时回收连接。
            return
        elif message_type == "data":
            tunnel_id = str(payload.get("tunnel_id") or "")
            writer = self._writers.get(tunnel_id)
            if writer is None:
                return
            try:
                writer.write(bytes.fromhex(str(payload.get("data") or "")))
                await writer.drain()
            except Exception:
                await self._close_tunnel(tunnel_id)
        elif message_type == "close":
            await self._close_tunnel(str(payload.get("tunnel_id") or ""))

    async def _close_tunnel(self, tunnel_id: str) -> None:
        writer = self._writers.pop(str(tunnel_id or ""), None)
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def _pipe_local_to_websocket(self, websocket: Any, reader: Any, writer: Any, tunnel_id: str) -> None:
        try:
            while True:
                chunk = await reader.read(64 * 1024)
                if not chunk:
                    break
                frame = tunnel_id.encode("utf-8") + b"\x00" + chunk
                await websocket.send(frame)
        finally:
            self._writers.pop(tunnel_id, None)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            await websocket.send(json.dumps({"type": "close", "tunnel_id": tunnel_id}, separators=(",", ":")))
