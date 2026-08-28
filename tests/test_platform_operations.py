from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
import app.web.platform_operations as platform_operations_web
from app.core.db import Base
from app.core.settings import settings
from app.models.entities import AlertRecord, AuditLog, HealthCheckRecord
from app.models.saas import PlatformAuditLog, PlatformUser, Tenant
from app.services.saas_auth import create_platform_session, hash_password
from app.web.auth_common import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE


@pytest.fixture()
def platform_operations_env(tmp_path, monkeypatch):
    """构造平台会话、全局运维数据和隔离日志目录。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'platform-operations.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "error.log").write_text(
        "2026-08-21 ignored error\n2026-08-21 target traceback\n",
        encoding="utf-8",
    )
    (log_dir / "app.log").write_text("2026-08-21 application ready\n", encoding="utf-8")

    monkeypatch.setattr(settings, "log_dir", str(log_dir))
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

    with factory() as db:
        tenant = Tenant(code="operations-tenant", name="运维测试企业", status="active")
        operator = PlatformUser(
            username="platform-operator",
            display_name="平台运维人员",
            password_hash=hash_password("Platform-operations-123"),
            role="platform_admin",
            must_change_password=False,
        )
        db.add_all([tenant, operator])
        db.flush()
        alert = AlertRecord(
            tenant_id=tenant.id,
            source="worker",
            level="warning",
            title="租户运维告警",
            message="worker heartbeat timeout",
            status="open",
        )
        db.add_all(
            [
                alert,
                HealthCheckRecord(
                    tenant_id=tenant.id,
                    scope="worker",
                    target_type="service",
                    status="ok",
                    summary="worker is healthy",
                ),
                AuditLog(
                    tenant_id=tenant.id,
                    actor="tenant-owner",
                    action="bot.update",
                    target_type="bot",
                    target_id=7,
                    detail="name changed",
                ),
            ]
        )
        db.commit()
        db.refresh(alert)
        created_session = create_platform_session(db, operator)
        alert_id = int(alert.id)

    yield {
        "factory": factory,
        "session": created_session,
        "alert_id": alert_id,
        "log_dir": Path(log_dir),
    }
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _set_platform_identity(client: TestClient, created_session) -> None:
    """按生产 Cookie Path 写入平台会话和对应 CSRF 原文。"""

    client.cookies.clear()
    client.cookies.set(PLATFORM_SESSION_COOKIE, created_session.raw_token, path="/platform")
    client.cookies.set(PLATFORM_CSRF_COOKIE, created_session.raw_csrf_token, path="/platform")


def test_platform_operations_pages_require_identity_and_share_secondary_navigation(platform_operations_env) -> None:
    """平台运维页面共用二级导航，租户 Cookie 不能替代平台员工身份。"""

    with TestClient(main_module.app, follow_redirects=False) as client:
        for path in ("/platform/operations", "/platform/protocol-services", "/platform/logs"):
            denied = client.get(path)
            assert denied.status_code == 303, path
            assert denied.headers["location"] == "/platform/login"

        _set_platform_identity(client, platform_operations_env["session"])
        monitoring = client.get("/platform/operations")
        protocol_services = client.get("/platform/protocol-services")
        logs = client.get("/platform/logs?tab=error&keyword=target")

        assert monitoring.status_code == 200
        assert "全局运行概览" in monitoring.text
        assert "租户运维告警" in monitoring.text
        assert protocol_services.status_code == 200
        assert "微信 iPad 协议" in protocol_services.text
        assert logs.status_code == 200
        assert "target traceback" in logs.text
        assert "ignored error" not in logs.text
        for response, active_href in (
            (monitoring, "/platform/operations"),
            (protocol_services, "/platform/protocol-services"),
            (logs, "/platform/logs"),
        ):
            primary_navigation = response.text.split('<nav class="app-primary-nav"', 1)[1].split("</nav>", 1)[0]
            operations_navigation = response.text.split('<nav class="section-tabs"', 1)[1].split("</nav>", 1)[0]
            assert ">协议服务</a>" not in primary_navigation
            assert 'href="/platform/operations" class="nav-link active"' in primary_navigation
            for label in ("运行监控", "协议服务", "全局日志"):
                assert label in operations_navigation
            assert f'class="section-tab active" href="{active_href}"' in operations_navigation


def test_platform_alert_resolution_requires_csrf_and_writes_platform_audit(platform_operations_env) -> None:
    """告警处理必须绑定平台 CSRF，成功后写平台审计而不是伪装租户操作者。"""

    alert_id = platform_operations_env["alert_id"]
    created_session = platform_operations_env["session"]
    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, created_session)
        rejected = client.post(
            f"/platform/operations/alerts/{alert_id}/resolve",
            data={"csrf_token": "forged"},
        )
        assert rejected.status_code == 403

        resolved = client.post(
            f"/platform/operations/alerts/{alert_id}/resolve",
            data={"csrf_token": created_session.raw_csrf_token},
        )
        assert resolved.status_code == 303
        assert resolved.headers["location"].startswith("/platform/operations?notice=")

    with platform_operations_env["factory"]() as db:
        alert = db.get(AlertRecord, alert_id)
        audit = db.scalar(
            select(PlatformAuditLog).where(
                PlatformAuditLog.action == "platform.alert.resolve",
                PlatformAuditLog.target_id == alert_id,
            )
        )
        assert alert.status == "resolved"
        assert alert.resolved_at is not None
        assert audit is not None
        assert audit.actor_user_id is not None
        assert audit.target_tenant_id == alert.tenant_id


@pytest.mark.parametrize("submitted_token", ["", "forged"])
def test_platform_restart_stops_at_csrf_boundary_without_running_script(
    platform_operations_env,
    monkeypatch,
    submitted_token: str,
) -> None:
    """重启测试只覆盖安全边界，缺失或伪造 CSRF 时绝不启动部署脚本。"""

    def fail_if_started(*_args, **_kwargs):
        pytest.fail("CSRF 校验失败后不应启动任何重启进程")

    monkeypatch.setattr(platform_operations_web.subprocess, "Popen", fail_if_started)
    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, platform_operations_env["session"])
        response = client.post(
            "/platform/operations/restart-app",
            data={"csrf_token": submitted_token},
        )

    assert response.status_code == 403
