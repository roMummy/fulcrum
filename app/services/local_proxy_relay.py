"""供 874 访问的临时 SOCKS5 中继监听器。

监听器只绑定本机回环地址和一次性登录凭证。每条 TCP 流都必须先命中当前扫码
会话，再由连接器把目标连接转到用户本机 SOCKS5，登录会话终态后凭证立即失效。
"""

from __future__ import annotations

import asyncio
import ipaddress
import struct
import uuid
from typing import Any

from app.core.settings import settings
from app.services.local_proxy_connector_ws import local_proxy_connector_ws_registry


class LocalProxyRelayServer:
    """生命周期绑定 FastAPI 进程的临时 SOCKS5 服务。"""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=settings.local_proxy_relay_host,
            port=int(settings.local_proxy_relay_port),
        )

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tunnel_id = uuid.uuid4().hex
        try:
            session_key = await self._authenticate(reader, writer)
            if not session_key:
                return
            target_host, target_port = await self._read_connect_request(reader, writer)
            tunnel = await local_proxy_connector_ws_registry.create_tunnel(
                tunnel_id=tunnel_id,
                session_key=session_key,
                writer=writer,
            )
            sent = await local_proxy_connector_ws_registry.send_session_message(
                session_key,
                {
                    "type": "open",
                    "session_key": session_key,
                    "tunnel_id": tunnel_id,
                    "target_host": target_host,
                    "target_port": target_port,
                },
            )
            if not sent:
                return
            try:
                opened, error = await asyncio.wait_for(tunnel.open_result, timeout=12)
            except asyncio.TimeoutError:
                opened, error = False, "connector_open_timeout"
            if not opened:
                await self._write_reply(writer, 0x05)
                return
            await self._write_reply(writer, 0x00)
            await self._pipe_server_to_connector(reader, session_key, tunnel_id)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            return
        finally:
            await local_proxy_connector_ws_registry.remove_tunnel(tunnel_id)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _authenticate(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str | None:
        version, count = await reader.readexactly(2)
        if version != 5:
            return None
        methods = await reader.readexactly(count)
        if 2 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return None
        writer.write(b"\x05\x02")
        await writer.drain()
        auth_version, username_length = await reader.readexactly(2)
        if auth_version != 1:
            return None
        username = (await reader.readexactly(username_length)).decode("utf-8", errors="ignore")
        password_length = (await reader.readexactly(1))[0]
        password = (await reader.readexactly(password_length)).decode("utf-8", errors="ignore")
        session_key = local_proxy_connector_ws_registry.find_session_by_credentials(username, password)
        writer.write(b"\x01\x00" if session_key else b"\x01\x01")
        await writer.drain()
        return session_key

    async def _read_connect_request(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[str, int]:
        version, command, _reserved, address_type = await reader.readexactly(4)
        if version != 5 or command != 1:
            await self._write_reply(writer, 0x07)
            raise ConnectionError("socks5_command_not_supported")
        if address_type == 1:
            host = str(ipaddress.ip_address(await reader.readexactly(4)))
        elif address_type == 3:
            host = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(host)).decode("idna", errors="ignore")
        elif address_type == 4:
            host = str(ipaddress.ip_address(await reader.readexactly(16)))
        else:
            await self._write_reply(writer, 0x08)
            raise ConnectionError("socks5_address_type_not_supported")
        port = struct.unpack("!H", await reader.readexactly(2))[0]
        return host, port

    async def _write_reply(self, writer: asyncio.StreamWriter, code: int) -> None:
        writer.write(b"\x05" + bytes([int(code)]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()

    async def _pipe_server_to_connector(self, reader: asyncio.StreamReader, session_key: str, tunnel_id: str) -> None:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                await local_proxy_connector_ws_registry.send_session_message(
                    session_key,
                    {"type": "close", "tunnel_id": tunnel_id},
                )
                return
            # 二进制帧格式：UTF-8 tunnel_id、0 分隔符、原始 TCP 数据。
            await local_proxy_connector_ws_registry.send_session_message(
                session_key,
                {"type": "data", "tunnel_id": tunnel_id, "data": chunk.hex()},
            )


local_proxy_relay_server = LocalProxyRelayServer()
