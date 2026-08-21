from __future__ import annotations

import importlib
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
from app.core.db import Base
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.saas import BillingOrder, BotBinding, PlatformAuditLog, PlatformUser, SaasPlan, Tenant, TenantSubscription, TenantUser
from app.services.billing import create_billing_order, submit_payment_evidence
from app.services.bot_entitlements import bind_bot_account
from app.services.saas_auth import create_platform_session, create_tenant_session, hash_password
from app.web.auth_common import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    TENANT_CSRF_COOKIE,
    TENANT_SESSION_COOKIE,
)


@pytest.fixture()
def platform_billing_env(tmp_path, monkeypatch):
    """构造待审核订单、平台会话和租户会话，覆盖两个身份空间的边界。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'platform-billing-pages.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "private-data"))
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

    for module_name in (
        "app.web.platform_billing",
        "app.web.platform_admin",
        "app.web.platform_auth",
        "app.web.tenant_auth",
    ):
        try:
            route_module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name == module_name:
                continue
            raise
        if hasattr(route_module, "SessionLocal"):
            monkeypatch.setattr(route_module, "SessionLocal", factory)

    with factory() as db:
        tenant = Tenant(code="platform-billing", name="待审核企业", status="active")
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
        reviewer = PlatformUser(
            username="reviewer",
            display_name="平台审核员",
            password_hash=hash_password("Reviewer-secret-123"),
            role="platform_admin",
            must_change_password=False,
        )
        tenant_user = TenantUser(
            tenant=tenant,
            username="owner",
            mobile="13800138000",
            display_name="租户所有者",
            password_hash=hash_password("Owner-secret-123"),
            role="tenant_admin",
            is_owner=True,
            must_change_password=False,
        )
        db.add_all([tenant, plan, reviewer, tenant_user])
        db.flush()
        db.add(
            TenantSubscription(
                tenant_id=tenant.id,
                plan_id=plan.id,
                status="active",
                starts_at=now_shanghai() - timedelta(days=5),
                expires_at=now_shanghai() + timedelta(days=5),
            )
        )
        db.commit()
        for record in (tenant, plan, reviewer, tenant_user):
            db.refresh(record)

        orders: list[BillingOrder] = []
        for cycle, reference in (("monthly", "TXN-PLATFORM-001"), ("yearly", "TXN-PLATFORM-002")):
            order = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle=cycle)
            order_dir = Path(settings.data_dir) / "tenant_billing" / str(tenant.id) / order.order_no
            order_dir.mkdir(parents=True, exist_ok=True)
            proof_path = order_dir / f"proof-{cycle}.png"
            proof_path.write_bytes(b"\x89PNG\r\n\x1a\nplatform-proof")
            relative_path = proof_path.relative_to(Path(settings.data_dir)).as_posix()
            submit_payment_evidence(
                db,
                tenant_id=tenant.id,
                order_id=order.id,
                payment_reference=reference,
                payment_proof_path=relative_path,
            )
            orders.append(order)

        platform_session = create_platform_session(db, reviewer)
        tenant_session = create_tenant_session(db, tenant_user)
        ids = {
            "tenant_id": tenant.id,
            "monthly_order_id": orders[0].id,
            "yearly_order_id": orders[1].id,
        }

    yield {
        "factory": factory,
        "platform_session": platform_session,
        "tenant_session": tenant_session,
        **ids,
    }
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _set_platform_identity(client: TestClient, created_session) -> None:
    """设置平台路径 Cookie，保持与生产 Path 属性一致。"""

    client.cookies.clear()
    client.cookies.set(PLATFORM_SESSION_COOKIE, created_session.raw_token, path="/platform")
    client.cookies.set(PLATFORM_CSRF_COOKIE, created_session.raw_csrf_token, path="/platform")


def test_tenant_cookie_cannot_access_platform_orders_and_detail_hides_storage_path(platform_billing_env) -> None:
    """租户 Cookie 不能进入平台审核；平台详情可下载凭证但不展示内部路径。"""

    with TestClient(main_module.app, follow_redirects=False) as client:
        client.cookies.set(TENANT_SESSION_COOKIE, platform_billing_env["tenant_session"].raw_token, path="/")
        client.cookies.set(TENANT_CSRF_COOKIE, platform_billing_env["tenant_session"].raw_csrf_token, path="/")
        denied = client.get("/platform/billing/orders")
        assert denied.status_code == 303
        assert denied.headers["location"] == "/platform/login"

        _set_platform_identity(client, platform_billing_env["platform_session"])
        listing = client.get("/platform/billing/orders")
        detail = client.get(f"/platform/billing/orders/{platform_billing_env['monthly_order_id']}")
        download = client.get(
            f"/platform/billing/orders/{platform_billing_env['monthly_order_id']}/proof"
        )

        assert listing.status_code == 200
        assert "待审核企业" in listing.text
        assert detail.status_code == 200
        assert "TXN-PLATFORM-001" in detail.text
        assert "tenant_billing/" not in detail.text
        assert download.status_code == 200
        assert download.content.startswith(b"\x89PNG")


def test_platform_approve_requires_csrf_and_is_idempotent(platform_billing_env) -> None:
    """批准必须使用平台 CSRF，重复批准只创建一条履约订阅和一条审计。"""

    order_id = platform_billing_env["monthly_order_id"]
    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, platform_billing_env["platform_session"])
        rejected_csrf = client.post(
            f"/platform/billing/orders/{order_id}/approve",
            data={"csrf_token": "forged"},
        )
        assert rejected_csrf.status_code == 403

        first = client.post(
            f"/platform/billing/orders/{order_id}/approve",
            data={"csrf_token": platform_billing_env["platform_session"].raw_csrf_token},
        )
        second = client.post(
            f"/platform/billing/orders/{order_id}/approve",
            data={"csrf_token": platform_billing_env["platform_session"].raw_csrf_token},
        )
        assert first.status_code == 303
        assert second.status_code == 303

    with platform_billing_env["factory"]() as db:
        order = db.get(BillingOrder, order_id)
        subscriptions = db.scalars(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == platform_billing_env["tenant_id"]
            )
        ).all()
        assert order.status == "approved"
        assert len(subscriptions) == 2
        assert db.scalar(
            select(func.count(PlatformAuditLog.id)).where(
                PlatformAuditLog.action == "billing_order.approve"
            )
        ) == 1


def test_platform_reject_requires_reason_and_records_audit(platform_billing_env) -> None:
    """驳回原因必填，成功驳回后订单可由租户重新提交并写平台审计。"""

    order_id = platform_billing_env["yearly_order_id"]
    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, platform_billing_env["platform_session"])
        missing_reason = client.post(
            f"/platform/billing/orders/{order_id}/reject",
            data={
                "csrf_token": platform_billing_env["platform_session"].raw_csrf_token,
                "reason": "",
            },
        )
        assert missing_reason.status_code == 303

        rejected = client.post(
            f"/platform/billing/orders/{order_id}/reject",
            data={
                "csrf_token": platform_billing_env["platform_session"].raw_csrf_token,
                "reason": "流水号与凭证不一致",
            },
        )
        assert rejected.status_code == 303

    with platform_billing_env["factory"]() as db:
        order = db.get(BillingOrder, order_id)
        assert order.status == "rejected"
        assert order.rejection_reason == "流水号与凭证不一致"
        assert db.scalar(
            select(func.count(PlatformAuditLog.id)).where(
                PlatformAuditLog.action == "billing_order.reject"
            )
        ) == 1


def test_platform_can_release_bot_binding_with_reason(platform_billing_env) -> None:
    """平台可以查看完整绑定并带原因释放，租户设备摘要和审计历史仍保留。"""

    with platform_billing_env["factory"]() as db, tenant_scope(platform_billing_env["tenant_id"]):
        binding = bind_bot_account(
            db,
            adapter_type="generic-api",
            bot_account_id="platform-release-bot",
            device_fingerprint="platform-release-device",
        )
        binding_id = binding.id

    with TestClient(main_module.app, follow_redirects=False) as client:
        _set_platform_identity(client, platform_billing_env["platform_session"])
        listing = client.get("/platform/bot-bindings")
        detail = client.get(f"/platform/bot-bindings/{binding_id}")
        forged = client.post(
            f"/platform/bot-bindings/{binding_id}/release",
            data={"csrf_token": "forged", "reason": "无效请求"},
        )
        released = client.post(
            f"/platform/bot-bindings/{binding_id}/release",
            data={
                "csrf_token": platform_billing_env["platform_session"].raw_csrf_token,
                "reason": "客户确认停用设备",
            },
        )

        assert listing.status_code == 200
        assert "platform-release-bot" in listing.text
        assert detail.status_code == 200
        assert "platform-release-device" not in detail.text
        assert forged.status_code == 403
        assert released.status_code == 303

    with platform_billing_env["factory"]() as db:
        stored = db.get(BotBinding, binding_id)
        assert stored.status == "released"
        assert stored.release_reason == "客户确认停用设备"
        assert db.scalar(
            select(func.count(PlatformAuditLog.id)).where(
                PlatformAuditLog.action == "bot_binding.release"
            )
        ) == 1
