from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.core.db as db_module
import app.main as main_module
from app.core.db import Base
from app.core.settings import settings
from app.models.saas import BillingOrder, BotBinding, PlatformUser, SaasPlan, TenantSubscription, TenantUser
from app.services.saas_auth import hash_password
from app.web.auth_common import PLATFORM_CSRF_COOKIE, TENANT_CSRF_COOKIE


OWNER_PASSWORD = "Commercial-owner-2026"
PLATFORM_PASSWORD = "Commercial-platform-2026"
RAW_DEVICE_FINGERPRINT = "commercial-flow-device-001"


@pytest.fixture()
def commercial_env(tmp_path, monkeypatch):
    """构造完整商业闭环所需的隔离数据库、私有文件目录和两类浏览器身份。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'public-commercial-flow.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # 所有可能在模块导入时缓存 SessionLocal 的 Router 都必须指向同一个临时数据库。
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
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "private-data"))
    monkeypatch.setattr(settings, "secret_key", "commercial-flow-secret-key")

    route_modules = {}
    for module_name in (
        "app.web.billing",
        "app.web.platform_billing",
        "app.web.public_site",
        "app.web.tenant_auth",
        "app.web.platform_auth",
    ):
        route_module = importlib.import_module(module_name)
        route_modules[module_name] = route_module
        if hasattr(route_module, "SessionLocal"):
            monkeypatch.setattr(route_module, "SessionLocal", factory)

    # 图形验证码的签名与图片生成已由独立测试覆盖；本测试固定验证码结果以聚焦商业事务链路。
    monkeypatch.setattr(route_modules["app.web.public_site"], "verify_captcha", lambda _token, _answer: True)

    with factory() as db:
        db.add_all(
            [
                SaasPlan(
                    code="trial",
                    name="免费试用",
                    provider_node_limit=1,
                    bot_limit=1,
                    monthly_price_cents=0,
                    yearly_price_cents=0,
                    trial_days=7,
                    is_public=False,
                    status="active",
                ),
                SaasPlan(
                    code="starter",
                    name="入门版",
                    provider_node_limit=1,
                    bot_limit=1,
                    monthly_price_cents=9_900,
                    yearly_price_cents=99_000,
                    is_public=True,
                    display_order=10,
                    status="active",
                ),
                PlatformUser(
                    username="commercial-admin",
                    display_name="商业审核管理员",
                    password_hash=hash_password(PLATFORM_PASSWORD),
                    role="platform_admin",
                    is_enabled=True,
                    must_change_password=False,
                ),
            ]
        )
        db.commit()

    yield {"factory": factory, "private_data": Path(settings.data_dir)}

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_public_registration_payment_approval_and_bot_quota_flow(commercial_env) -> None:
    """公开注册到付费订阅和额度拦截必须在隔离环境形成可重复的真实闭环。"""

    factory = commercial_env["factory"]
    with (
        TestClient(main_module.app, follow_redirects=False) as tenant_client,
        TestClient(main_module.app, follow_redirects=False) as platform_client,
    ):
        pricing = tenant_client.get("/pricing")
        assert pricing.status_code == 200
        assert "入门版" in pricing.text
        assert "¥99.00" in pricing.text

        registration = tenant_client.post(
            "/register",
            data={
                "mobile": "18800000002",
                "password": OWNER_PASSWORD,
                "confirm_password": OWNER_PASSWORD,
                "enterprise_name": "商业闭环测试企业",
                "contact_name": "测试企业主",
                "captcha_token": "isolated-test-token",
                "captcha_answer": "12345",
            },
        )
        assert registration.status_code == 303
        assert registration.headers["location"] == "/account/onboarding"
        tenant_csrf = tenant_client.cookies.get(TENANT_CSRF_COOKIE)
        assert tenant_csrf

        onboarding = tenant_client.get("/account/onboarding")
        assert onboarding.status_code == 200
        assert "免费试用" in onboarding.text

        with factory() as db:
            owner = db.scalar(select(TenantUser).where(TenantUser.mobile == "18800000002"))
            starter = db.scalar(select(SaasPlan).where(SaasPlan.code == "starter"))
            trial_subscription = db.scalar(
                select(TenantSubscription).where(TenantSubscription.tenant_id == owner.tenant_id)
            )
            assert owner is not None and starter is not None and trial_subscription is not None
            assert trial_subscription.plan.code == "trial"
            tenant_id = owner.tenant_id
            starter_id = starter.id

        created_order = tenant_client.post(
            "/account/orders",
            data={
                "csrf_token": tenant_csrf,
                "plan_id": str(starter_id),
                "billing_cycle": "monthly",
            },
        )
        assert created_order.status_code == 303
        order_path = created_order.headers["location"]
        order_id = int(order_path.rsplit("/", 1)[-1])

        submitted_payment = tenant_client.post(
            f"{order_path}/pay",
            data={"csrf_token": tenant_csrf, "payment_reference": "COMMERCIAL-TEST-001"},
            files={
                "payment_proof": (
                    "customer-original-name.png",
                    b"\x89PNG\r\n\x1a\ncommercial-test-proof",
                    "image/png",
                )
            },
        )
        assert submitted_payment.status_code == 303

        platform_login = platform_client.post(
            "/platform/login",
            data={"username": "commercial-admin", "password": PLATFORM_PASSWORD},
        )
        assert platform_login.status_code == 303
        platform_csrf = platform_client.cookies.get(PLATFORM_CSRF_COOKIE)
        assert platform_csrf

        approved = platform_client.post(
            f"/platform/billing/orders/{order_id}/approve",
            data={"csrf_token": platform_csrf},
        )
        assert approved.status_code == 303

        first_binding = tenant_client.post(
            "/account/bot-bindings",
            data={
                "csrf_token": tenant_csrf,
                "adapter_type": "generic-api",
                "bot_account_id": "commercial-bot-001",
                "display_name": "商业测试机器人",
                "device_fingerprint": RAW_DEVICE_FINGERPRINT,
            },
        )
        assert first_binding.status_code == 303
        assert "tone=danger" not in first_binding.headers["location"]

        over_quota = tenant_client.post(
            "/account/bot-bindings",
            data={
                "csrf_token": tenant_csrf,
                "adapter_type": "generic-api",
                "bot_account_id": "commercial-bot-002",
                "display_name": "超额机器人",
                "device_fingerprint": RAW_DEVICE_FINGERPRINT,
            },
        )
        assert over_quota.status_code == 303
        assert "tone=danger" in over_quota.headers["location"]

    with factory() as db:
        order = db.get(BillingOrder, order_id)
        active_subscription = db.scalar(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == tenant_id,
                TenantSubscription.status == "active",
            )
        )
        bindings = db.scalars(select(BotBinding).where(BotBinding.tenant_id == tenant_id)).all()

        assert order is not None and order.status == "approved"
        assert order.amount_cents == 9_900
        assert order.bot_limit_snapshot == 1
        assert order.fulfilled_subscription_id == active_subscription.id
        assert active_subscription.plan.code == "starter"
        assert len(bindings) == 1
        assert bindings[0].bot_account_id == "commercial-bot-001"
        assert bindings[0].device_fingerprint_hash != RAW_DEVICE_FINGERPRINT

        # 私有路径只能保存随机文件名；数据库和磁盘都不能复用客户上传的原文件名。
        assert order.payment_proof_path
        assert "customer-original-name" not in order.payment_proof_path
        proof_path = commercial_env["private_data"] / order.payment_proof_path
        assert proof_path.is_file()
        assert db.scalar(select(func.count(BotBinding.id)).where(BotBinding.tenant_id == tenant_id)) == 1

    database_bytes = (commercial_env["private_data"].parent / "public-commercial-flow.db").read_bytes()
    assert RAW_DEVICE_FINGERPRINT.encode("utf-8") not in database_bytes
