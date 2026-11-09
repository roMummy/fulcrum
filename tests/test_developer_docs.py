from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request
from starlette.responses import RedirectResponse

def _load_route():
    """在独立运行时准备可写测试环境，合并运行时复用先加载的隔离数据库。"""

    if "app.core.db" not in sys.modules:
        test_root = Path(".run/test-runtime/developer-docs").resolve()
        test_root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("DATA_DIR", str(test_root))
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{test_root / 'test.db'}")
        log_dir = test_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("LOG_DIR", str(log_dir))
    from app.web.developer_docs import developer_docs_page

    return developer_docs_page


def _request(path: str = "/developer/docs") -> Request:
    """构造不依赖 FastAPI TestClient 的最小 HTTP 请求上下文。"""

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 12345),
            "headers": [],
        }
    )


def _principal() -> SimpleNamespace:
    """构造页面渲染所需的可信租户身份快照。"""

    return SimpleNamespace(
        session_id=1,
        tenant_id=1,
        user_id=1,
        username="admin",
        display_name="文档管理员",
        role="tenant_admin",
        must_change_password=False,
        tenant_status="active",
        subscription_status="active",
        subscription_expires_at=None,
        is_owner=True,
    )


def test_developer_docs_requires_tenant_login() -> None:
    """文档中心只对企业会话开放，未登录不能通过收藏地址直接读取。"""

    request = _request()
    request.state.current_user = None

    response = asyncio.run(_load_route()(request))

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_developer_docs_renders_bridge_api_content() -> None:
    """登录后页面上下文必须包含推荐桥接接口和开发者鉴权说明。"""

    request = _request()
    request.state.current_user = _principal()
    request._cookies = {"mbs_tenant_csrf": "test-csrf"}

    response = asyncio.run(_load_route()(request))

    assert response.status_code == 200
    assert response.template.name == "developer_docs.html"
    html = str(response.context["document_html"])
    assert "/api/open/messages" in html
    assert "X-Signature" in html
    assert "扫码登录" in html
    # 左侧目录的每个条目都必须能在正文里找到对应锚点，保证点击可跳转。
    toc = str(response.context["document_toc"])
    assert 'href="#鉴权hmac-sha256"' in toc
    assert 'id="鉴权hmac-sha256"' in html
    navigation = response.context["account_nav_items"]
    docs_item = next(item for item in navigation if item["href"] == "/developer/docs")
    assert docs_item["active"] is True
