from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
import app.services.login_proxy_pool as proxy_service
import app.web.platform_operations as platform_operations_web
from app.core.db import Base
from app.models.saas import PlatformAuditLog, PlatformProxy, PlatformUser
from app.services.saas_auth import create_platform_session, hash_password
from app.web.auth_common import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE


class _ConnectedSocket:
    """模拟成功建立的 TCP 连接，避免平台代理页面测试访问真实网络。"""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


@pytest.fixture()
def platform_proxy_env(tmp_path, monkeypatch):
    """构造只包含平台员工和代理池表的隔离应用环境。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'platform-proxy-pool.db'}",
        connect_args={"check_same_thread": False},
    )
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(db_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(platform_operations_web, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "init_db_with_seed", lambda _db: None)
    monkeypatch.setattr(main_module, "is_setup_required", lambda _db: False)
    monkeypatch.setattr(
        main_module,
        "database_readiness",
        lambda: {"ready": True, "error": "", "expected_revision": "test-head"},
    )
    monkeypatch.setattr(main_module, "start_background_workers", lambda: None)
    monkeypatch.setattr(main_module, "stop_background_workers", lambda: None)
    monkeypatch.setattr(proxy_service.socket, "create_connection", lambda *_args, **_kwargs: _ConnectedSocket())

    with factory() as db:
        user = PlatformUser(
            username="proxy-admin",
            display_name="代理管理员",
            password_hash=hash_password("Proxy-admin-123"),
            role="platform_admin",
            is_enabled=True,
            auth_version=1,
            must_change_password=False,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        created_session = create_platform_session(db, user)

    yield {"factory": factory, "session": created_session}
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _set_platform_identity(client: TestClient, created_session) -> None:
    """按平台 Cookie 路径写入会话和 CSRF 原文。"""

    client.cookies.clear()
    client.cookies.set(PLATFORM_SESSION_COOKIE, created_session.raw_token, path="/platform")
    client.cookies.set(PLATFORM_CSRF_COOKIE, created_session.raw_csrf_token, path="/platform")


def test_platform_proxy_page_requires_platform_identity_and_uses_operations_navigation(
    platform_proxy_env,
) -> None:
    """代理池必须属于系统运维二级导航，未登录请求不能读取平台资源。"""

    with TestClient(main_module.app, follow_redirects=False) as client:
        denied = client.get("/platform/proxy-pool")
        assert denied.status_code == 303
        assert denied.headers["location"] == "/platform/login"

        _set_platform_identity(client, platform_proxy_env["session"])
        page = client.get("/platform/proxy-pool")

    assert page.status_code == 200
    primary_navigation = page.text.split('<nav class="app-primary-nav"', 1)[1].split("</nav>", 1)[0]
    operations_navigation = page.text.split('<nav class="section-tabs"', 1)[1].split("</nav>", 1)[0]
    assert ">代理池</a>" not in primary_navigation
    assert 'href="/platform/operations" class="nav-link active"' in primary_navigation
    for label in ("运行监控", "协议服务", "代理池", "全局日志"):
        assert label in operations_navigation
    assert 'class="section-tab active" href="/platform/proxy-pool"' in operations_navigation
    for label in ("代理总数", "健康可用", "停用或异常", "当前占用"):
        assert label in page.text


def test_platform_proxy_actions_require_csrf_and_never_render_password(platform_proxy_env) -> None:
    """平台写操作必须校验 CSRF，保存后页面和审计均不包含代理密码。"""

    created_session = platform_proxy_env["session"]
    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, created_session)
        rejected = client.post(
            "/platform/proxy-pool",
            data={
                "csrf_token": "forged",
                "name": "杭州代理",
                "province": "浙江省",
                "city": "杭州市",
                "host": "10.10.10.10",
                "port": "1080",
                "username": "proxy-user",
                "password": "page-secret",
            },
        )
        assert rejected.status_code == 403

        saved = client.post(
            "/platform/proxy-pool",
            data={
                "csrf_token": created_session.raw_csrf_token,
                "name": "杭州代理",
                "province": "浙江省",
                "city": "杭州市",
                "host": "10.10.10.10",
                "port": "1080",
                "username": "proxy-user",
                "password": "page-secret",
            },
        )
        assert saved.status_code == 303
        page = client.get("/platform/proxy-pool")
        assert page.status_code == 200
        assert "杭州代理" in page.text
        assert "10.10.*.*:1080" in page.text
        assert "page-secret" not in page.text

    with platform_proxy_env["factory"]() as db:
        proxy = db.scalar(select(PlatformProxy).where(PlatformProxy.name == "杭州代理"))
        audit = db.scalar(
            select(PlatformAuditLog).where(PlatformAuditLog.action == "platform.proxy.save")
        )
        assert proxy is not None
        assert proxy.password_encrypted != "page-secret"
        assert audit is not None
        assert "page-secret" not in (audit.detail or "")
        assert "10.10.10.10" not in (audit.detail or "")


def test_platform_proxy_import_toggle_and_check_use_controlled_routes(platform_proxy_env) -> None:
    """批量导入、启停和检查都复用平台认证边界并写入稳定状态。"""

    created_session = platform_proxy_env["session"]
    csv_content = (
        "名称,省,市,主机,端口,用户名,密码\n"
        "上海代理,上海市,上海市,10.20.20.20,1080,proxy-user,csv-secret\n"
    ).encode("utf-8")
    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, created_session)
        imported = client.post(
            "/platform/proxy-pool/import",
            data={"csrf_token": created_session.raw_csrf_token},
            files={"csv_file": ("proxies.csv", csv_content, "text/csv")},
        )
        assert imported.status_code == 303

    with platform_proxy_env["factory"]() as db:
        proxy = db.scalar(select(PlatformProxy).where(PlatformProxy.name == "上海代理"))
        proxy_id = int(proxy.id)

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, created_session)
        disabled = client.post(
            f"/platform/proxy-pool/{proxy_id}/toggle",
            data={"csrf_token": created_session.raw_csrf_token, "is_enabled": "false"},
        )
        checked = client.post(
            f"/platform/proxy-pool/{proxy_id}/check",
            data={"csrf_token": created_session.raw_csrf_token},
        )
        assert disabled.status_code == 303
        assert checked.status_code == 303

    with platform_proxy_env["factory"]() as db:
        proxy = db.get(PlatformProxy, proxy_id)
        assert proxy.is_enabled is False
        assert proxy.health_status == "healthy"
        assert proxy.last_checked_at is not None
