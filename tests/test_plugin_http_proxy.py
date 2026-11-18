"""PluginHttpClient 的 HTTP/SOCKS 代理、绕过和凭据测试。"""

from __future__ import annotations

import base64
import json
import socket
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request

import pytest

import app.services.plugin_runtime.http_client as http_client_module
from app.services.plugin_runtime.http_client import (
    PluginHttpClient,
    PluginHttpError,
    _open_socks_socket,
)
from app.services.plugin_runtime.proxy import (
    current_plugin_proxy,
    parse_proxy_url,
    plugin_proxy_scope,
)


@contextmanager
def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class _QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args, **_kwargs):  # noqa: ANN002
        return


def _proxy_server():
    records: list[dict[str, object]] = []

    class Handler(_QuietHandler):
        redirect_to = ""

        def do_GET(self):  # noqa: N802
            records.append({"path": self.path, "headers": dict(self.headers)})
            if Handler.redirect_to:
                self.send_response(302)
                self.send_header("Location", Handler.redirect_to)
                self.end_headers()
                return
            body = json.dumps({"ok": True, "via": "proxy"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return records, Handler


def _target_server():
    records: list[str] = []

    class Handler(_QuietHandler):
        def do_GET(self):  # noqa: N802
            records.append(self.path)
            body = b"target-ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return records, Handler


def test_http_proxy_forwards_absolute_url_and_auth_header() -> None:
    records, proxy_handler = _proxy_server()
    with _serve(proxy_handler) as proxy:
        endpoint = parse_proxy_url(
            f"http://proxy-user:proxy-pass@127.0.0.1:{proxy.server_port}"
        )
        response = PluginHttpClient(proxy=endpoint).request(
            "GET",
            "http://target.example.test/data?x=1",
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "via": "proxy"}
    assert records and records[0]["path"].startswith("http://target.example.test/data")
    authorization = records[0]["headers"].get("Proxy-Authorization", "")
    assert authorization.startswith("Basic ")
    assert base64.b64decode(authorization.split(" ", 1)[1]).decode() == "proxy-user:proxy-pass"


def test_https_request_through_http_proxy_prepares_connect_tunnel() -> None:
    endpoint = parse_proxy_url("http://proxy.example.test:8080")
    handler = http_client_module._BypassProxyHandler(endpoint)
    request = Request("https://secure.example.test/path")
    handler.proxy_open(request, endpoint.url, "https")
    assert request._tunnel_host == "secure.example.test"  # noqa: SLF001
    assert request.host == "proxy.example.test:8080"


def test_loopback_and_configured_hosts_bypass_proxy() -> None:
    proxy_records, proxy_handler = _proxy_server()
    target_records, target_handler = _target_server()
    with _serve(proxy_handler) as proxy, _serve(target_handler) as target:
        endpoint = parse_proxy_url(
            f"http://127.0.0.1:{proxy.server_port}", bypass=".internal.test"
        )
        response = PluginHttpClient(proxy=endpoint).request(
            "GET",
            f"http://127.0.0.1:{target.server_port}/direct",
        )

    assert response.text == "target-ok"
    assert target_records == ["/direct"]
    assert proxy_records == []


def test_redirect_to_bypassed_host_uses_direct_connection() -> None:
    proxy_records, proxy_handler = _proxy_server()
    target_records, target_handler = _target_server()
    with _serve(proxy_handler) as proxy, _serve(target_handler) as target:
        proxy_handler.redirect_to = f"http://127.0.0.1:{target.server_port}/redirected"
        endpoint = parse_proxy_url(f"http://127.0.0.1:{proxy.server_port}")
        response = PluginHttpClient(proxy=endpoint).request(
            "GET",
            "http://origin.example.test/start",
        )

    assert response.text == "target-ok"
    assert len(proxy_records) == 1
    assert target_records == ["/redirected"]


class _FakeSocks:
    SOCKS5 = 3
    calls: list[tuple] = []

    @staticmethod
    def create_connection(address, timeout=None, **kwargs):  # noqa: ANN001
        _FakeSocks.calls.append((address, timeout, kwargs))
        return socket.create_connection(("127.0.0.1", address[1]), timeout=timeout)


def test_socks5_handler_passes_proxy_credentials_and_dns_mode(monkeypatch) -> None:
    _FakeSocks.calls.clear()
    monkeypatch.setattr(http_client_module, "_import_socks", lambda: _FakeSocks)
    target_records, target_handler = _target_server()
    with _serve(target_handler) as target:
        endpoint = parse_proxy_url(
            f"socks5h://socks-user:socks-pass@127.0.0.1:1080"
        )
        response = PluginHttpClient(proxy=endpoint).request(
            "GET",
            f"http://target.example.test:{target.server_port}/socks",
        )

    assert response.text == "target-ok"
    assert target_records == ["/socks"]
    address, timeout, kwargs = _FakeSocks.calls[0]
    assert address == ("target.example.test", target.server_port)
    assert timeout is not None
    assert kwargs["proxy_addr"] == "127.0.0.1"
    assert kwargs["proxy_port"] == 1080
    assert kwargs["proxy_rdns"] is True
    assert (kwargs["proxy_username"], kwargs["proxy_password"]) == ("socks-user", "socks-pass")


def test_socks5_uses_local_dns_and_proxy_scope_is_restored(monkeypatch) -> None:
    _FakeSocks.calls.clear()
    monkeypatch.setattr(http_client_module, "_import_socks", lambda: _FakeSocks)
    target_records, target_handler = _target_server()
    endpoint = parse_proxy_url("socks5://127.0.0.1:1080")
    with _serve(target_handler) as target:
        with plugin_proxy_scope(endpoint):
            assert current_plugin_proxy() is endpoint
            response = PluginHttpClient().request(
                "GET",
                f"http://target.example.test:{target.server_port}/local-dns",
            )
        assert current_plugin_proxy() is None

    assert response.text == "target-ok"
    assert target_records == ["/local-dns"]
    assert _FakeSocks.calls[0][2]["proxy_rdns"] is False


def test_missing_pysocks_returns_clear_error(monkeypatch) -> None:
    endpoint = parse_proxy_url("socks5://127.0.0.1:1080")
    monkeypatch.setitem(sys.modules, "socks", None)
    with pytest.raises(PluginHttpError, match="代理组件未安装"):
        _open_socks_socket(endpoint, "example.test", 80, 1.0)
