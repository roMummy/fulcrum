"""本地代理连接器的进程内 WebSocket 注册表。

连接器只在扫码登录期间被使用。注册表不持久化 WebSocket，也不持久化本地 SOCKS
凭证；进程重启后所有旧连接自然失效，登录会话由过期任务回收。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LocalConnectorSocket:
    """一个已经完成设备密钥认证的连接器 WebSocket。"""

    connector_id: int
    tenant_id: int
    websocket: Any
    sessions: set[str] = field(default_factory=set)


@dataclass(slots=True)
class LocalProxyTunnel:
    """874 与连接器之间的一条临时 TCP 转发流。"""

    tunnel_id: str
    session_key: str
    writer: Any
    open_result: asyncio.Future[tuple[bool, str]]


class LocalProxyConnectorWebSocketRegistry:
    """按连接器和登录会话隔离 WebSocket，拒绝跨设备发送控制消息。"""

    def __init__(self) -> None:
        self._by_connector: dict[int, LocalConnectorSocket] = {}
        self._by_session: dict[str, int] = {}
        # 临时中继用户名和密码只在服务进程内存在，登录会话终态或进程退出时都会删除。
        self._session_credentials: dict[str, tuple[str, str]] = {}
        self._tunnels: dict[str, LocalProxyTunnel] = {}
        self._lock = asyncio.Lock()

    def register_session_credentials(self, session_key: str, username: str, password: str) -> None:
        """登记扫码会话中继凭证；数据库只保存摘要。"""

        normalized = str(session_key or "").strip()
        if not normalized or not username or not password:
            raise ValueError("connector_relay_credentials_required")
        self._session_credentials[normalized] = (str(username), str(password))

    def get_session_credentials(self, session_key: str) -> tuple[str, str] | None:
        """读取当前进程中的中继凭证，供临时 SOCKS 监听器校验。"""

        return self._session_credentials.get(str(session_key or "").strip())

    def remove_session_credentials(self, session_key: str) -> None:
        """登录会话结束后立即丢弃中继凭证。"""

        normalized = str(session_key or "").strip()
        self._session_credentials.pop(normalized, None)
        connector_id = self._by_session.pop(normalized, None)
        if connector_id is not None and connector_id in self._by_connector:
            self._by_connector[connector_id].sessions.discard(normalized)

    def find_session_by_credentials(self, username: str, password: str) -> str | None:
        """SOCKS 认证只允许命中当前进程中登记的单次扫码凭证。"""

        for session_key, credentials in self._session_credentials.items():
            if credentials == (str(username), str(password)):
                return session_key
        return None

    async def create_tunnel(self, *, tunnel_id: str, session_key: str, writer: Any) -> LocalProxyTunnel:
        """登记一条服务端 SOCKS 连接，并返回等待连接器确认的 Future。"""

        loop = asyncio.get_running_loop()
        tunnel = LocalProxyTunnel(
            tunnel_id=str(tunnel_id),
            session_key=str(session_key),
            writer=writer,
            open_result=loop.create_future(),
        )
        async with self._lock:
            self._tunnels[tunnel.tunnel_id] = tunnel
        return tunnel

    async def resolve_open_result(self, tunnel_id: str, *, ok: bool, error: str = "") -> None:
        """接收连接器对 `open` 的确认，禁止越过未知隧道写入数据。"""

        async with self._lock:
            tunnel = self._tunnels.get(str(tunnel_id or ""))
        if tunnel is not None and not tunnel.open_result.done():
            tunnel.open_result.set_result((bool(ok), str(error or "")))

    async def send_tunnel_data(self, tunnel_id: str, data: bytes) -> None:
        """把连接器二进制数据写回 874 TCP 连接。"""

        async with self._lock:
            tunnel = self._tunnels.get(str(tunnel_id or ""))
        if tunnel is None:
            return
        tunnel.writer.write(bytes(data))
        await tunnel.writer.drain()

    async def remove_tunnel(self, tunnel_id: str) -> None:
        async with self._lock:
            tunnel = self._tunnels.pop(str(tunnel_id or ""), None)
        if tunnel is not None and not tunnel.open_result.done():
            tunnel.open_result.set_result((False, "tunnel_closed"))

    async def register(self, connection: LocalConnectorSocket) -> None:
        async with self._lock:
            old = self._by_connector.pop(connection.connector_id, None)
            if old is not None:
                try:
                    await old.websocket.close(code=4001)
                except Exception:
                    pass
            self._by_connector[connection.connector_id] = connection

    async def unregister(self, connector_id: int, websocket: Any) -> set[str]:
        async with self._lock:
            current = self._by_connector.get(int(connector_id))
            if current is None or current.websocket is not websocket:
                return set()
            self._by_connector.pop(int(connector_id), None)
            sessions = set(current.sessions)
            for session_key in sessions:
                self._by_session.pop(session_key, None)
            return sessions

    async def bind_session(self, connector_id: int, session_key: str) -> None:
        normalized = str(session_key or "").strip()
        if not normalized:
            raise ValueError("connector_session_required")
        async with self._lock:
            connection = self._by_connector.get(int(connector_id))
            if connection is None:
                raise ValueError("connector_offline")
            connection.sessions.add(normalized)
            self._by_session[normalized] = int(connector_id)

    async def unbind_session(self, session_key: str) -> None:
        normalized = str(session_key or "").strip()
        async with self._lock:
            connector_id = self._by_session.pop(normalized, None)
            if connector_id is None:
                return
            connection = self._by_connector.get(connector_id)
            if connection is not None:
                connection.sessions.discard(normalized)
            self._session_credentials.pop(normalized, None)

    async def send_session_end(self, session_key: str, *, reason: str) -> bool:
        normalized = str(session_key or "").strip()
        async with self._lock:
            connector_id = self._by_session.get(normalized)
            connection = self._by_connector.get(connector_id) if connector_id else None
        if connection is None:
            return False
        await connection.websocket.send_text(
            json.dumps(
                {"type": "session_end", "session_key": normalized, "reason": str(reason or "")},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return True

    async def send_session_message(self, session_key: str, payload: dict[str, Any]) -> bool:
        """向当前扫码会话发送受控控制消息，调用方不得把本地代理凭证放进 payload。"""

        normalized = str(session_key or "").strip()
        async with self._lock:
            connector_id = self._by_session.get(normalized)
            connection = self._by_connector.get(connector_id) if connector_id else None
        if connection is None:
            return False
        await connection.websocket.send_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return True

    def is_online(self, connector_id: int) -> bool:
        """同步读取在线连接；页面只在当前进程内使用该摘要，持久状态仍以心跳为准。"""

        return int(connector_id) in self._by_connector


local_proxy_connector_ws_registry = LocalProxyConnectorWebSocketRegistry()
