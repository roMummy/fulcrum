from __future__ import annotations

import importlib
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
from app.core.db import Base
from app.core.settings import settings
from app.core.time import now_shanghai
from app.models.saas import BillingOrder, SaasPlan, Tenant, TenantSubscription, TenantUser
from app.services.billing import create_billing_order
from app.services.saas_auth import create_tenant_session, hash_password
from app.web.auth_common import TENANT_CSRF_COOKIE, TENANT_SESSION_COOKIE


@pytest.fixture()
def billing_env(tmp_path, monkeypatch):
    """构造两个租户和两类角色，验证页面权限、文件隔离与 CSRF。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'tenant-billing-pages.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "private-data"))
    monkeypatch.setattr(settings, "billing_payment_proof_max_bytes", 64)
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(db_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "TenantSessionLocal", factory)
    monkeypatch.setattr(main_module, "init_db_with_seed", lambda _db: None)
    monkeypatch.setattr(main_module, "is_setup_required", lambda _db: False)
    monkeypatch.setattr(
        main_module,
        "database_readiness",
        lambda: {"ready": True, "error": "", "expected_revision": "test-head"},
    )
    monkeypatch.setattr(main_module, "start_background_workers", lambda: None)
    monkeypatch.setattr(main_module, "stop_background_workers", lambda: None)

    for module_name in ("app.web.billing", "app.web.public_site", "app.web.tenant_auth"):
        try:
            route_module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
        if hasattr(route_module, "SessionLocal"):
            monkeypatch.setattr(route_module, "SessionLocal", factory)

    with factory() as db:
        plan = SaasPlan(
            code="standard",
            name="标准版",
            provider_node_limit=3,
            bot_limit=3,
            monthly_price_cents=25_900,
            yearly_price_cents=259_000,
            is_public=True,
            status="active",
        )
        first = Tenant(code="billing-page-a", name="页面企业甲", status="active")
        second = Tenant(code="billing-page-b", name="页面企业乙", status="active")
        db.add_all([plan, first, second])
        db.flush()
        owner = TenantUser(
            tenant_id=first.id,
            username="owner",
            mobile="13800138000",
            display_name="企业所有者",
            password_hash=hash_password("Owner-secret-123"),
            role="tenant_admin",
            is_owner=True,
            must_change_password=False,
        )
        readonly = TenantUser(
            tenant_id=first.id,
            username="viewer",
            display_name="只读员工",
            password_hash=hash_password("Viewer-secret-123"),
            role="readonly",
            must_change_password=False,
        )
        second_owner = TenantUser(
            tenant_id=second.id,
            username="owner",
            mobile="13900139000",
            display_name="其他企业主",
            password_hash=hash_password("Second-secret-123"),
            role="tenant_admin",
            is_owner=True,
            must_change_password=False,
        )
        db.add_all([owner, readonly, second_owner])
        db.flush()
        for tenant in (first, second):
            db.add(
                TenantSubscription(
                    tenant_id=tenant.id,
                    plan_id=plan.id,
                    status="active",
                    starts_at=now_shanghai(),
                    expires_at=now_shanghai() + timedelta(days=30),
                )
            )
        db.commit()
        for user in (owner, readonly, second_owner):
            db.refresh(user)
        owner_session = create_tenant_session(db, owner)
        readonly_session = create_tenant_session(db, readonly)
        second_session = create_tenant_session(db, second_owner)
        ids = {
            "first_tenant_id": first.id,
            "second_tenant_id": second.id,
            "plan_id": plan.id,
        }

    yield {
        "factory": factory,
        "owner_session": owner_session,
        "readonly_session": readonly_session,
        "second_session": second_session,
        **ids,
    }
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _set_identity(client: TestClient, created_session) -> None:
    """给测试浏览器设置同一租户会话配套的 Session 与 CSRF Cookie。"""

    client.cookies.clear()
    client.cookies.set(TENANT_SESSION_COOKIE, created_session.raw_token, path="/")
    client.cookies.set(TENANT_CSRF_COOKIE, created_session.raw_csrf_token, path="/")


def test_owner_can_create_order_and_readonly_employee_can_only_view(billing_env) -> None:
    """管理员可创建订单，只读员工可查看但不能发起购买。"""

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_identity(client, billing_env["owner_session"])
        page = client.get("/account/billing")
        created = client.post(
            "/account/orders",
            data={
                "csrf_token": billing_env["owner_session"].raw_csrf_token,
                "plan_id": str(billing_env["plan_id"]),
                "billing_cycle": "monthly",
            },
        )
        assert page.status_code == 200
        assert "标准版" in page.text
        assert created.status_code == 303
        assert created.headers["location"].startswith("/account/orders/")

        _set_identity(client, billing_env["readonly_session"])
        readonly_page = client.get(created.headers["location"])
        denied = client.post(
            "/account/orders",
            data={
                "csrf_token": billing_env["readonly_session"].raw_csrf_token,
                "plan_id": str(billing_env["plan_id"]),
                "billing_cycle": "yearly",
            },
        )
        assert readonly_page.status_code == 200
        assert denied.status_code == 403


def test_other_tenant_order_returns_not_found(billing_env) -> None:
    """即使知道订单主键，其他租户也只能得到 404。"""

    with billing_env["factory"]() as db:
        order = create_billing_order(
            db,
            tenant_id=billing_env["second_tenant_id"],
            plan_id=billing_env["plan_id"],
            billing_cycle="monthly",
        )
        order_id = order.id

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_identity(client, billing_env["owner_session"])
        response = client.get(f"/account/orders/{order_id}")
        assert response.status_code == 404


def test_payment_proof_is_private_validated_and_downloaded_through_auth_route(billing_env) -> None:
    """付款凭证使用私有路径保存，页面不泄露路径，下载必须经过租户订单鉴权。"""

    with billing_env["factory"]() as db:
        order = create_billing_order(
            db,
            tenant_id=billing_env["first_tenant_id"],
            plan_id=billing_env["plan_id"],
            billing_cycle="monthly",
        )
        order_id = order.id

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_identity(client, billing_env["owner_session"])
        missing_csrf = client.post(
            f"/account/orders/{order_id}/pay",
            data={"payment_reference": "TXN-NO-CSRF"},
            files={"payment_proof": ("proof.png", b"\x89PNG\r\n\x1a\nvalid", "image/png")},
        )
        assert missing_csrf.status_code == 403

        uploaded = client.post(
            f"/account/orders/{order_id}/pay",
            data={
                "csrf_token": billing_env["owner_session"].raw_csrf_token,
                "payment_reference": "TXN-PRIVATE-001",
            },
            files={"payment_proof": ("customer-name.png", b"\x89PNG\r\n\x1a\nvalid", "image/png")},
        )
        assert uploaded.status_code == 303

        with billing_env["factory"]() as db:
            stored = db.get(BillingOrder, order_id)
            assert stored.payment_proof_path
            assert "customer-name" not in stored.payment_proof_path
            assert not Path(stored.payment_proof_path).is_absolute()
            private_file = Path(settings.data_dir) / stored.payment_proof_path
            assert private_file.is_file()
            stored_path = stored.payment_proof_path

        detail = client.get(f"/account/orders/{order_id}")
        download = client.get(f"/account/orders/{order_id}/proof")
        assert detail.status_code == 200, detail.text
        assert stored_path not in detail.text
        assert "已提交付款凭证，等待审核" in detail.text
        assert download.status_code == 200
        assert download.content.startswith(b"\x89PNG")


@pytest.mark.parametrize(
    ("filename", "content_type", "content"),
    [
        ("proof.exe", "application/octet-stream", b"not-allowed"),
        ("proof.png", "image/png", b"not-a-png"),
        ("proof.pdf", "application/pdf", b"%PDF-" + b"x" * 80),
    ],
)
def test_payment_proof_rejects_invalid_type_signature_or_size(
    billing_env,
    filename: str,
    content_type: str,
    content: bytes,
) -> None:
    """非法扩展名、伪造文件头和超过上限的凭证均不能写入订单。"""

    with billing_env["factory"]() as db:
        order = create_billing_order(
            db,
            tenant_id=billing_env["first_tenant_id"],
            plan_id=billing_env["plan_id"],
            billing_cycle="monthly",
        )
        order_id = order.id

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_identity(client, billing_env["owner_session"])
        response = client.post(
            f"/account/orders/{order_id}/pay",
            data={
                "csrf_token": billing_env["owner_session"].raw_csrf_token,
                "payment_reference": "TXN-INVALID",
            },
            files={"payment_proof": (filename, content, content_type)},
        )
        assert response.status_code == 400

    with billing_env["factory"]() as db:
        stored = db.get(BillingOrder, order_id)
        assert stored.status == "pending_payment"
        assert stored.payment_proof_path is None


def test_cancel_order_requires_matching_tenant_csrf(billing_env) -> None:
    """订单取消属于状态变更，缺少配套 CSRF 时必须直接拒绝。"""

    with billing_env["factory"]() as db:
        order = create_billing_order(
            db,
            tenant_id=billing_env["first_tenant_id"],
            plan_id=billing_env["plan_id"],
            billing_cycle="yearly",
        )
        order_id = order.id

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_identity(client, billing_env["owner_session"])
        response = client.post(f"/account/orders/{order_id}/cancel", data={"csrf_token": ""})
        assert response.status_code == 403


def test_manual_bot_binding_routes_are_removed_from_tenant_product(billing_env) -> None:
    """租户不能再手填账号或设备身份，绑定只能由统一账号接入服务创建。"""

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_identity(client, billing_env["owner_session"])
        page = client.get("/account/billing")
        removed_get = client.get("/account/bot-bindings")
        removed_post = client.post(
            "/account/bot-bindings",
            data={
                "csrf_token": billing_env["owner_session"].raw_csrf_token,
                "adapter_type": "telegram",
                "bot_account_id": "forged-account",
                "device_fingerprint": "forged-device",
            },
        )
        assert page.status_code == 200
        assert removed_get.status_code == 404
        assert removed_post.status_code == 404
        for removed_text in ("机器人绑定", "adapter_type", "bot_account_id", "device_fingerprint"):
            assert removed_text not in page.text
