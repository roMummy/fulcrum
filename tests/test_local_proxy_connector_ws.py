from __future__ import annotations

import asyncio

from app.services.local_proxy_connector_ws import (
    LocalConnectorSocket,
    LocalProxyConnectorWebSocketRegistry,
)


class _FakeSocket:
    """只记录控制帧，验证注册表不会把消息发到错误设备。"""

    def __init__(self):
        self.messages: list[str] = []
        self.closed: list[int] = []

    async def send_text(self, payload: str) -> None:
        self.messages.append(payload)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def test_registry_binds_session_and_releases_credentials() -> None:
    """扫码会话只绑定当前连接器，解绑后临时中继凭证立即消失。"""

    async def run() -> None:
        registry = LocalProxyConnectorWebSocketRegistry()
        socket = _FakeSocket()
        registry.register_session_credentials("session-1", "user", "pass")
        await registry.register(LocalConnectorSocket(connector_id=1, tenant_id=10, websocket=socket))
        await registry.bind_session(1, "session-1")

        assert registry.find_session_by_credentials("user", "pass") == "session-1"
        assert await registry.send_session_end("session-1", reason="completed") is True
        assert "session_end" in socket.messages[0]

        await registry.unbind_session("session-1")
        assert registry.find_session_by_credentials("user", "pass") is None

    asyncio.run(run())


def test_disconnect_returns_bound_sessions() -> None:
    """连接器断开时返回全部绑定会话，调用方可统一标记登录失败并释放租约。"""

    async def run() -> None:
        registry = LocalProxyConnectorWebSocketRegistry()
        socket = _FakeSocket()
        await registry.register(LocalConnectorSocket(connector_id=2, tenant_id=11, websocket=socket))
        await registry.bind_session(2, "session-a")
        await registry.bind_session(2, "session-b")

        sessions = await registry.unregister(2, socket)

        assert sessions == {"session-a", "session-b"}
        assert registry.is_online(2) is False

    asyncio.run(run())
