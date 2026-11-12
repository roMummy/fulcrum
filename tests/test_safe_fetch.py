"""出站媒体下载的 SSRF 防护:只允许公网地址,重定向的每一跳也要校验。"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import safe_fetch
from app.services.safe_fetch import UnsafeUrlError, ensure_public_http_url, open_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x", "http://localhost/x", "http://127.1/x", "http://[::1]/x",
        "http://10.0.0.5/x", "http://192.168.50.10:8280/x", "http://172.16.3.4/x",
        "http://169.254.169.254/latest/meta-data/",          # 云元数据
        "http://0.0.0.0/x", "http://[::ffff:127.0.0.1]/x",   # IPv4 映射的回环
        "http://[fe80::1]/x", "http://224.0.0.1/x",
    ],
)
def test_internal_and_reserved_addresses_are_refused(url) -> None:
    with pytest.raises(UnsafeUrlError):
        ensure_public_http_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "http:///x", "", "http://user:pw@example.com/x"])
def test_other_schemes_and_embedded_credentials_are_refused(url) -> None:
    with pytest.raises(UnsafeUrlError):
        ensure_public_http_url(url)


def test_a_hostname_that_resolves_to_an_internal_address_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", port))])
    with pytest.raises(UnsafeUrlError, match="内网"):
        ensure_public_http_url("http://innocent-looking.example.com/x")
    # 只要有一个解析结果是内网就拒绝(攻击者可以同时返回公网和内网地址)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)), (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))])
    with pytest.raises(UnsafeUrlError):
        ensure_public_http_url("http://mixed.example.com/x")


def test_a_public_address_is_allowed(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])
    assert ensure_public_http_url("https://cdn.example.com/a.png") == "https://cdn.example.com/a.png"


def test_a_redirect_to_an_internal_address_is_blocked_on_that_hop(monkeypatch) -> None:
    """公网地址 302 到内网:第一跳通过,但重定向目标必须被拦下。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *args):  # 静默
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    real = safe_fetch.ensure_public_http_url
    # 测试服务器在回环上:仅对第一跳放行,重定向目标仍走真实校验
    first = {"done": False}

    def fake(url):
        if not first["done"] and url.startswith(f"http://127.0.0.1:{port}"):
            first["done"] = True
            return url
        return real(url)

    monkeypatch.setattr(safe_fetch, "ensure_public_http_url", fake)
    try:
        with pytest.raises(Exception, match="重定向目标不安全"):
            open_public_url(f"http://127.0.0.1:{port}/start")
    finally:
        server.shutdown()


def test_the_gugatool_adapter_refuses_internal_media_urls() -> None:
    from app.services.channel_adapters.wechat_gugatool import WechatGugatoolAdapter

    adapter = WechatGugatoolAdapter()
    for url in ("http://192.168.50.10:8280/secret", "http://127.0.0.1:9064/", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(RuntimeError, match="地址不允许"):
            adapter._load_media_bytes({"image_url": url}, url_keys=("image_url",), b64_keys=("base64",))
