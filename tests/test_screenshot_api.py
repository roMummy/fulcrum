"""截图 HTTP Router 的真实鉴权、租户上下文及图片响应，使用独立临时库。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import tool_api
from app.core.db import Base
from app.core.tenant_context import require_tenant_id
from app.models.saas import Tenant
from app.services.web_screenshots import ScreenshotError, ScreenshotResult


@pytest.fixture()
def screenshot_client(tmp_path, monkeypatch):
    """只挂载真实工具 Router，不启动后台 Worker，不使用真实应用数据库。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'screenshots.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        active = Tenant(code="screenshot-test", name="截图测试企业", status="active")
        db.add_all([active, Tenant(code="disabled-test", name="停用测试企业", status="disabled")])
        db.commit()
        tenant_id = active.id
    monkeypatch.setattr(tool_api, "SessionLocal", factory)
    monkeypatch.setattr(tool_api.settings, "tool_api_key", "test-screenshot-key")
    app = FastAPI()
    app.include_router(tool_api.router)
    with TestClient(app) as client:
        yield client, tenant_id
    engine.dispose()


def _headers():
    return {"X-API-Key": "test-screenshot-key", "X-Tenant-Code": "screenshot-test"}


@pytest.mark.parametrize("headers,status", [({}, 401), ({"X-API-Key": "wrong", "X-Tenant-Code": "screenshot-test"}, 401), ({"X-API-Key": "test-screenshot-key"}, 400), ({"X-API-Key": "test-screenshot-key", "X-Tenant-Code": "disabled-test"}, 401)])
def test_rejects_invalid_key_missing_or_inactive_tenant(screenshot_client, monkeypatch, headers, status):
    """未通过工具密钥和有效租户双重认证时，不能进入截图服务。"""

    client, _tenant_id = screenshot_client
    monkeypatch.setattr(tool_api, "capture_screenshot", lambda _: pytest.fail("未认证请求不能截图"))
    assert client.post("/api/tool/screenshots", json={"html": "<p>test</p>"}, headers=headers).status_code == status


def test_returns_image_bytes_and_thread_pool_inherits_authenticated_tenant(screenshot_client, monkeypatch):
    """成功直接返回图片，当前租户来自认证结果；HTML 不经过另一条独立截图实现。"""

    client, tenant_id = screenshot_client
    calls = []

    def capture(request):
        calls.append((require_tenant_id(), request.html))
        return ScreenshotResult(b"test-image-content", "image/png", 640, 480, 12)

    monkeypatch.setattr(tool_api, "capture_screenshot", capture)
    response = client.post("/api/tool/screenshots", json={"html": "<h1>测试</h1>", "width": 640, "height": 480}, headers=_headers())
    assert response.status_code == 200 and response.content == b"test-image-content"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-screenshot-width"] == "640" and response.headers["x-screenshot-height"] == "480"
    assert calls == [(tenant_id, "<h1>测试</h1>")]


@pytest.mark.parametrize("body", [{"html": "test", "tenant_id": 999}, {"html": "test", "engine_url": "ws://other"}, {"url": "https://example.com", "html": "test"}, {"html": "test", "format": "pdf"}])
def test_rejects_identity_engine_override_and_invalid_source(screenshot_client, body):
    """请求只能表达截图意图，不能修改服务、租户或支持的输出格式。"""

    client, _tenant_id = screenshot_client
    assert client.post("/api/tool/screenshots", json=body, headers=_headers()).status_code == 422


@pytest.mark.parametrize("code,status", [("screenshot_busy", 429), ("screenshot_timeout", 504), ("screenshot_not_configured", 503), ("screenshot_too_large", 413), ("screenshot_engine_error", 502)])
def test_stable_service_errors_preserve_http_meaning(screenshot_client, monkeypatch, code, status):
    """故障保持业务语义，不能把超时、繁忙与未配置都伪装成成功图片。"""

    client, _tenant_id = screenshot_client

    def failed(_request):
        raise ScreenshotError(code, "截图错误测试", status)

    monkeypatch.setattr(tool_api, "capture_screenshot", failed)
    response = client.post("/api/tool/screenshots", json={"html": "test"}, headers=_headers())
    assert response.status_code == status and response.json()["detail"]["code"] == code
