"""最小 SOCKS5 客户端，只连接用户配置的本机代理。"""

from __future__ import annotations

import asyncio
import ipaddress
import struct


async def open_socks5_connection(
    host: str,
    port: int,
    target_host: str,
    target_port: int,
    *,
    username: str = "",
    password: str = "",
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """通过本机 SOCKS5 代理建立目标 TCP 连接。"""

    reader, writer = await asyncio.open_connection(host, int(port))
    methods = b"\x00" if not username and not password else b"\x02"
    writer.write(b"\x05" + bytes([len(methods)]) + methods)
    await writer.drain()
    version, method = await reader.readexactly(2)
    if version != 5 or method == 0xFF:
        writer.close()
        await writer.wait_closed()
        raise ConnectionError("socks5_auth_method_rejected")
    if method == 2:
        user_bytes = username.encode("utf-8")
        password_bytes = password.encode("utf-8")
        writer.write(b"\x01" + bytes([len(user_bytes)]) + user_bytes + bytes([len(password_bytes)]) + password_bytes)
        await writer.drain()
        auth_version, auth_status = await reader.readexactly(2)
        if auth_version != 1 or auth_status != 0:
            writer.close()
            await writer.wait_closed()
            raise ConnectionError("socks5_credentials_rejected")
    try:
        address = ipaddress.ip_address(target_host)
        atyp = 1 if address.version == 4 else 4
        encoded_host = address.packed
    except ValueError:
        encoded_host = target_host.encode("idna")
        atyp = 3
        encoded_host = bytes([len(encoded_host)]) + encoded_host
    writer.write(b"\x05\x01\x00" + bytes([atyp]) + encoded_host + struct.pack("!H", int(target_port)))
    await writer.drain()
    response = await reader.readexactly(4)
    if response[1] != 0:
        writer.close()
        await writer.wait_closed()
        raise ConnectionError(f"socks5_connect_failed:{response[1]}")
    if response[3] == 1:
        await reader.readexactly(4)
    elif response[3] == 3:
        length = (await reader.readexactly(1))[0]
        await reader.readexactly(length)
    elif response[3] == 4:
        await reader.readexactly(16)
    await reader.readexactly(2)
    return reader, writer
