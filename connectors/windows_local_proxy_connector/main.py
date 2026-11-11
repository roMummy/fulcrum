"""Windows 本地代理扫码连接器命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from urllib import request

from config import ConnectorConfigStore
from tunnel import LocalProxyConnectorClient


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fulcrum Windows 本地代理扫码连接器")
    parser.add_argument("--server", required=True, help="SaaS 地址，例如 https://saas.example.com")
    parser.add_argument("--activation-code", default="", help="首次绑定使用的一次性激活码")
    parser.add_argument("--socks-host", default="127.0.0.1")
    parser.add_argument("--socks-port", type=int, default=1080)
    parser.add_argument("--socks-username", default="")
    parser.add_argument("--socks-password", default="")
    return parser.parse_args()


def _activate(args: argparse.Namespace, store: ConnectorConfigStore) -> dict[str, object]:
    """调用 SaaS 激活接口并把设备密钥交给本地受保护存储。"""

    device_id = "windows-" + uuid.uuid4().hex
    body = json.dumps(
        {
            "activation_code": args.activation_code,
            "device_id": device_id,
            "client_version": "0.1.0",
            "local_proxy_label": f"SOCKS5 {args.socks_host}:{args.socks_port}",
        }
    ).encode("utf-8")
    req = request.Request(
        args.server.rstrip("/") + "/api/local-connector/activate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    store.save_device_key(
        server=args.server,
        connector_id=int(payload["connector_id"]),
        device_key=str(payload["device_key"]),
        device_id=device_id,
    )
    return payload


async def _run() -> None:
    args = _parse_args()
    store = ConnectorConfigStore()
    config = store.load()
    if args.activation_code:
        _activate(args, store)
        config = store.load()
    device_key = store.device_key()
    if not device_key:
        raise SystemExit("请先提供 --activation-code 完成首次绑定")
    client = LocalProxyConnectorClient(
        server=str(config.get("server") or args.server),
        device_key=device_key,
        socks={
            "host": args.socks_host,
            "port": args.socks_port,
            "username": args.socks_username,
            "password": args.socks_password,
        },
    )
    await client.run()


if __name__ == "__main__":
    asyncio.run(_run())
