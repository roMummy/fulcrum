"""公共截图只连接受控假 CDP，不访问真实网页或复用任何业务浏览器状态。"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
import threading

from PIL import Image
from pydantic import ValidationError
import pytest

from app.core.tenant_context import TenantScopeError, tenant_scope
from app.services import web_screenshots as screenshots


def _image(width=640, height=480, format="PNG"):
    buffer = BytesIO()
    Image.new("RGB", (width, height), "#dce8f1").save(buffer, format=format)
    return buffer.getvalue()


class FakeCdp:
    """按真实 CDP 信封交错事件与命令响应，记录会话创建、截图参数和释放。"""

    def __init__(self, number):
        self.number = number
        self.commands = []
        self.responses = []
        self.width, self.height = 640, 480
        self.content_height = 1360
        self.closed = False
        self.fail_method = ""
        self.hang_method = ""
        self.invalid_image = False
        self.invalid_frame_tree = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def send(self, encoded):
        command = json.loads(encoded)
        self.commands.append(command)
        method, params = command["method"], command["params"]
        result = {}
        if method == "Target.createBrowserContext":
            result = {"browserContextId": f"context-{self.number}"}
        elif method == "Target.createTarget":
            result = {"targetId": f"target-{self.number}"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": f"session-{self.number}"}
        elif method == "Emulation.setDeviceMetricsOverride":
            self.width, self.height = params["width"], params["height"]
        elif method == "Page.getFrameTree":
            result = {"frameTree": [] if self.invalid_frame_tree else {"frame": {"id": f"frame-{self.number}"}}}
        elif method == "Page.getLayoutMetrics":
            result = {"cssContentSize": {"width": self.width, "height": self.content_height}}
        elif method == "Kitesurf.hasPendingCriticalResources":
            result = {"pending": False}
        elif method == "Page.captureScreenshot":
            clip = params["clip"]
            content = _image(1, 1) if self.invalid_image else _image(clip["width"], clip["height"], params["format"].upper())
            result = {"data": base64.b64encode(content).decode()}
        if method == self.hang_method:
            return
        response = {"id": command["id"], "result": result}
        if method == self.fail_method:
            # 引擎原文故意含敏感信息，验证公共错误不会把它返回调用方。
            response = {"id": command["id"], "error": {"message": "secret-token=private-test-value"}}
        self.responses.extend([{"method": "Page.lifecycleEvent", "params": {"name": "load"}}, response])

    async def recv(self):
        if self.responses:
            return json.dumps(self.responses.pop(0))
        await asyncio.sleep(10)


@pytest.fixture()
def fake_engine(monkeypatch):
    """所有测试使用单独额度与独占假连接，避免依赖应用初始化时的配置。"""

    connections = []
    monkeypatch.setattr(screenshots.settings, "kitesurf_ws_url", "ws://kitesurf.test/devtools/browser")
    monkeypatch.setattr(screenshots, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(screenshots, "ensure_public_http_url", lambda url: url)

    def connect(url, **kwargs):
        assert url == "ws://kitesurf.test/devtools/browser"
        assert kwargs["proxy"] is None and kwargs["max_size"] == screenshots.MAX_CDP_MESSAGE_BYTES
        connection = FakeCdp(len(connections) + 1)
        connections.append(connection)
        return connection

    monkeypatch.setattr(screenshots, "websocket_connect", connect)
    return connections


@pytest.mark.parametrize("source", [{}, {"url": "https://example.com", "html": "<p>双来源</p>"}, {"html": "  "}, {"url": ""}, {"html": "x", "tenant_id": 2}, {"html": "x", "width": 9000}, {"html": "x", "full_page": "false"}])
def test_request_rejects_missing_ambiguous_or_untrusted_fields(source):
    """同一模型保护 Python 与 HTTP 入口，不允许外部指定租户和引擎等字段。"""

    with pytest.raises(ValidationError):
        screenshots.ScreenshotRequest(**source)


def test_capture_requires_trusted_tenant_scope(fake_engine):
    """未建立可信作用域时不能占用浏览器或截图额度。"""

    with pytest.raises(TenantScopeError):
        screenshots.capture_screenshot(screenshots.ScreenshotRequest(html="<p>test</p>"))
    assert fake_engine == []


@pytest.mark.parametrize("format", ["png", "jpeg"])
def test_url_full_page_uses_global_engine_and_standard_image_reply(fake_engine, format):
    """完整走 URL 协议序列、事件分离、整页尺寸和可发送图片结果。"""

    with tenant_scope(1):
        result = screenshots.capture_screenshot(screenshots.ScreenshotRequest(url="https://example.com/", width=640, height=480, format=format))
    assert (result.width, result.height, result.media_type) == (640, 1360, f"image/{format}")
    assert base64.b64decode(result.as_image_reply()["base64"]) == result.content
    connection = fake_engine[0]
    methods = [command["method"] for command in connection.commands]
    assert "Page.navigate" in methods and "Page.setDocumentContent" not in methods
    captured = next(item for item in connection.commands if item["method"] == "Page.captureScreenshot")
    assert captured["params"]["clip"] == {"x": 0, "y": 0, "width": 640, "height": 1360, "scale": 1}
    assert ("quality" in captured["params"]) is (format == "jpeg")
    assert connection.commands[-1]["method"] == "Target.disposeBrowserContext"
    assert connection.closed


def test_html_viewport_and_successive_tenants_get_distinct_contexts(fake_engine):
    """不同租户及重复调用不会共用 Cookie、页面和 frame，也不依赖旧调用字段。"""

    for tenant_id in (1, 2, 1):
        with tenant_scope(tenant_id):
            result = screenshots.capture_screenshot(screenshots.ScreenshotRequest(html=f"<p>租户 {tenant_id}</p>", width=640, height=480, full_page=False))
        assert (result.width, result.height) == (640, 480)
    for connection in fake_engine:
        methods = [command["method"] for command in connection.commands]
        assert "Page.navigate" not in methods and "Page.getLayoutMetrics" not in methods
        loaded = next(item for item in connection.commands if item["method"] == "Page.setDocumentContent")
        assert loaded["params"]["frameId"] == f"frame-{connection.number}"
        created = next(item for item in connection.commands if item["method"] == "Target.createTarget")
        assert created["params"]["browserContextId"] == f"context-{connection.number}"
        assert connection.closed


@pytest.mark.parametrize("variant,expected", [("engine", "screenshot_engine_error"), ("image", "screenshot_image_invalid"), ("large", "screenshot_too_large"), ("frame", "screenshot_protocol_error")])
def test_failures_release_context_and_quota_without_leaking_raw_error(fake_engine, monkeypatch, variant, expected):
    """渲染失败、占位图、整页超限与坏协议均有稳定错误，下一次调用仍能成功。"""

    original = screenshots.websocket_connect

    def faulty(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.fail_method = "Page.captureScreenshot" if variant == "engine" else ""
        connection.invalid_image = variant == "image"
        connection.content_height = 10000 if variant == "large" else 1360
        connection.invalid_frame_tree = variant == "frame"
        return connection

    monkeypatch.setattr(screenshots, "websocket_connect", faulty)
    with tenant_scope(1), pytest.raises(screenshots.ScreenshotError) as failed:
        screenshots.capture_screenshot(screenshots.ScreenshotRequest(html="<p>test</p>", width=640, height=480))
    assert failed.value.code == expected
    assert "private-test-value" not in str(failed.value)
    assert fake_engine[0].closed and fake_engine[0].commands[-1]["method"] == "Target.disposeBrowserContext"
    monkeypatch.setattr(screenshots, "websocket_connect", original)
    with tenant_scope(1):
        assert screenshots.capture_screenshot(screenshots.ScreenshotRequest(html="ok", width=640, height=480)).content


def test_timeout_cleans_up_and_does_not_hold_the_next_request(fake_engine, monkeypatch):
    """总体超时后仍释放连接和额度，不把后续截图永久阻塞。"""

    original = screenshots.websocket_connect

    def hanging(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.hang_method = "Page.captureScreenshot"
        return connection

    monkeypatch.setattr(screenshots, "websocket_connect", hanging)
    with tenant_scope(1), pytest.raises(screenshots.ScreenshotError) as failed:
        screenshots.capture_screenshot(screenshots.ScreenshotRequest(html="test", width=640, height=480, timeout_seconds=1))
    assert failed.value.code == "screenshot_timeout" and fake_engine[0].closed
    assert fake_engine[0].commands[-1]["method"] == "Target.disposeBrowserContext"
    monkeypatch.setattr(screenshots, "websocket_connect", original)
    with tenant_scope(1):
        assert screenshots.capture_screenshot(screenshots.ScreenshotRequest(html="ok", width=640, height=480)).content


def test_busy_rejects_immediately_without_opening_a_connection(fake_engine):
    """截图容量耗尽不再往消息处理线程里堆积等待任务。"""

    assert screenshots._slots.acquire(blocking=False)
    try:
        with tenant_scope(1), pytest.raises(screenshots.ScreenshotError) as failed:
            screenshots.capture_screenshot(screenshots.ScreenshotRequest(html="test"))
        assert failed.value.code == "screenshot_busy" and failed.value.status_code == 429
        assert fake_engine == []
    finally:
        screenshots._slots.release()


def test_private_target_and_missing_engine_are_explicit_errors(fake_engine, monkeypatch):
    """引擎可位于内网，用户截图目标仍须通过公网校验，错误不回退其他引擎。"""

    from app.services.safe_fetch import ensure_public_http_url

    monkeypatch.setattr(screenshots, "ensure_public_http_url", ensure_public_http_url)
    with tenant_scope(1), pytest.raises(screenshots.ScreenshotError) as failed:
        screenshots.capture_screenshot(screenshots.ScreenshotRequest(url="http://127.0.0.1/private"))
    assert failed.value.code == "screenshot_url_invalid"
    monkeypatch.setattr(screenshots.settings, "kitesurf_ws_url", "")
    with tenant_scope(1), pytest.raises(screenshots.ScreenshotError) as failed:
        screenshots.capture_screenshot(screenshots.ScreenshotRequest(html="test"))
    assert failed.value.code == "screenshot_not_configured"
    assert fake_engine == []


@pytest.mark.parametrize("source,format", [("html", "png"), ("url", "jpeg")])
def test_local_websocket_round_trip_uses_real_transport(monkeypatch, source, format):
    """本机真实 WebSocket 握手与文本帧传输，验证异步客户端实际可与 CDP 服务交互。"""

    from websockets.sync.server import serve

    connections = []

    def handler(socket):
        connection = FakeCdp(len(connections) + 1)
        connections.append(connection)
        for frame in socket:
            asyncio.run(connection.send(frame))
            while connection.responses:
                socket.send(json.dumps(connection.responses.pop(0)))

    monkeypatch.setattr(screenshots, "ensure_public_http_url", lambda url: url)
    monkeypatch.setattr(screenshots, "_slots", threading.BoundedSemaphore(1))
    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        monkeypatch.setattr(screenshots.settings, "kitesurf_ws_url", f"ws://127.0.0.1:{port}/devtools/browser")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            content = {source: "https://example.com" if source == "url" else "<h1>中文截图</h1>"}
            with tenant_scope(1):
                result = screenshots.capture_screenshot(screenshots.ScreenshotRequest(**content, width=640, height=480, format=format))
            assert result.media_type == f"image/{format}" and (result.width, result.height) == (640, 1360)
            assert connections[0].commands[-1]["method"] == "Target.disposeBrowserContext"
        finally:
            server.shutdown()
            worker.join(timeout=3)
