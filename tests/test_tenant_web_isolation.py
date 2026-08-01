from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
from app.core.db import Base
from app.models.entities import Bot
from app.models.saas import Tenant, TenantUser
from app.services.saas_auth import hash_password


TENANT_PASSWORD = "Tenant-isolation-123"


@pytest.fixture()
def tenant_web_context(tmp_path, monkeypatch):
    """用真实中间件和临时数据库验证 URL 主键不会绕过租户过滤。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'tenant-web.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 主应用先用平台会话恢复登录身份，再用租户会话处理页面。两个工厂必须同时
    # 指向本用例数据库，否则测试会失去对真实中间件边界的覆盖。
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(db_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "init_db_with_seed", lambda _db: None)
    monkeypatch.setattr(main_module, "is_setup_required", lambda _db: False)
    monkeypatch.setattr(main_module, "start_background_workers", lambda: None)
    monkeypatch.setattr(main_module, "stop_background_workers", lambda: None)

    for module_name in ("app.web.tenant_auth", "app.web.platform_auth"):
        route_module = importlib.import_module(module_name)
        monkeypatch.setattr(route_module, "SessionLocal", factory)

    with factory() as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        db.add_all([alpha, beta])
        db.flush()
        db.add(
            TenantUser(
                tenant_id=alpha.id,
                username="admin",
                display_name="甲管理员",
                role="tenant_admin",
                password_hash=hash_password(TENANT_PASSWORD),
                is_enabled=True,
                auth_version=1,
                must_change_password=False,
            )
        )
        alpha_bot = Bot(tenant_id=alpha.id, name="甲租户机器人")
        beta_bot = Bot(tenant_id=beta.id, name="乙租户机密机器人")
        db.add_all([alpha_bot, beta_bot])
        db.commit()
        db.refresh(alpha_bot)
        db.refresh(beta_bot)
        ids = {"alpha_bot": alpha_bot.id, "beta_bot": beta_bot.id}

    with TestClient(main_module.app, follow_redirects=False) as client:
        login = client.post(
            "/login",
            data={"tenant_code": "alpha", "username": "admin", "password": TENANT_PASSWORD},
        )
        assert login.status_code == 303
        yield client, ids

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_tenant_list_only_contains_current_tenant_rows(tenant_web_context) -> None:
    """列表查询由租户会话自动过滤，不能渲染其他租户的名称。"""

    client, _ids = tenant_web_context
    response = client.get("/bots")

    assert response.status_code == 200
    assert "甲租户机器人" in response.text
    assert "乙租户机密机器人" not in response.text


def test_tenant_cannot_open_or_edit_other_tenant_bot(tenant_web_context) -> None:
    """猜测其他租户主键时，读取和写入入口都必须表现为资源不存在。"""

    client, ids = tenant_web_context
    detail = client.get(f"/bots/{ids['beta_bot']}")
    edit = client.post(
        f"/bots/{ids['beta_bot']}/edit",
        data={"name": "越权改名", "description": "", "is_enabled": "true"},
    )

    assert detail.status_code == 404
    assert "乙租户机密机器人" not in detail.text
    assert edit.status_code == 404
