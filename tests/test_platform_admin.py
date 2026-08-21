from __future__ import annotations

from datetime import timedelta
import re

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.services.platform_admin as platform_admin
import app.web.platform_admin as platform_admin_web
from app.core.db import Base
from app.core.time import now_shanghai
from app.models.saas import PlatformAuditLog, PlatformUser, SaasPlan, Tenant, TenantSubscription, TenantUser
from app.services.platform_admin import PlatformAdminError
from app.services.saas_auth import create_platform_session, hash_password, load_platform_principal, verify_password
from app.web.auth_common import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE


@pytest.fixture()
def db(tmp_path):
    """平台服务测试使用独立数据库，事务失败后可以精确检查残留记录。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'platform-admin.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _create_plan(db: Session, *, status: str = "active") -> SaasPlan:
    """创建只限制 Provider 节点数的首期套餐。"""

    plan = SaasPlan(
        code=f"plan-{status}",
        name=f"{status} 套餐",
        provider_node_limit=2,
        status=status,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def test_platform_admin_creates_tenant_subscription_and_initial_admin_atomically(db: Session) -> None:
    """租户、订阅、首个管理员和审计必须在一个成功事务中同时存在。"""

    plan = _create_plan(db)
    expires_at = now_shanghai() + timedelta(days=365)
    created = platform_admin.create_tenant_with_subscription(
        db,
        code="Acme-Corp",
        name="示例企业",
        plan_id=plan.id,
        expires_at=expires_at,
        admin_username="admin",
        admin_display_name="企业管理员",
        actor_user_id=None,
    )

    tenant = db.get(Tenant, created.tenant_id)
    admin = db.get(TenantUser, created.admin_user_id)
    subscription = db.scalar(select(TenantSubscription).where(TenantSubscription.tenant_id == tenant.id))
    audit = db.scalar(select(PlatformAuditLog).where(PlatformAuditLog.target_tenant_id == tenant.id))

    assert tenant.code == "acme-corp"
    assert subscription.plan_id == plan.id
    assert admin.role == "tenant_admin"
    assert admin.must_change_password is True
    assert verify_password(created.initial_password, admin.password_hash)
    assert created.initial_password not in (audit.detail or "")


def test_platform_admin_rejects_disabled_plan_and_duplicate_tenant_code(db: Session) -> None:
    """停用套餐不能开通新租户，租户编码在大小写归一后仍必须全局唯一。"""

    disabled_plan = _create_plan(db, status="disabled")
    with pytest.raises(PlatformAdminError, match="plan_disabled"):
        platform_admin.create_tenant_with_subscription(
            db,
            code="blocked",
            name="不可开通租户",
            plan_id=disabled_plan.id,
            expires_at=now_shanghai() + timedelta(days=30),
            admin_username="admin",
            admin_display_name="管理员",
        )

    active_plan = SaasPlan(code="active-plan", name="启用套餐", provider_node_limit=1, status="active")
    db.add(active_plan)
    db.commit()
    platform_admin.create_tenant_with_subscription(
        db,
        code="same-code",
        name="首个租户",
        plan_id=active_plan.id,
        expires_at=None,
        admin_username="admin",
        admin_display_name="管理员",
    )
    with pytest.raises(PlatformAdminError, match="tenant_code_exists"):
        platform_admin.create_tenant_with_subscription(
            db,
            code="SAME-CODE",
            name="重复租户",
            plan_id=active_plan.id,
            expires_at=None,
            admin_username="admin",
            admin_display_name="管理员",
        )


def test_tenant_creation_rolls_back_all_rows_when_admin_password_hash_fails(db: Session, monkeypatch) -> None:
    """管理员凭证创建失败时不得留下孤立租户或订阅。"""

    plan = _create_plan(db)

    def fail_hash(_password: str) -> str:
        raise RuntimeError("password_hash_failed")

    monkeypatch.setattr(platform_admin, "hash_password", fail_hash)
    with pytest.raises(RuntimeError, match="password_hash_failed"):
        platform_admin.create_tenant_with_subscription(
            db,
            code="rollback-me",
            name="事务回滚租户",
            plan_id=plan.id,
            expires_at=None,
            admin_username="admin",
            admin_display_name="管理员",
        )

    assert db.scalar(select(func.count(Tenant.id)).where(Tenant.code == "rollback-me")) == 0
    assert db.scalar(select(func.count(TenantSubscription.id))) == 0
    assert db.scalar(select(func.count(TenantUser.id))) == 0


def test_save_plan_persists_public_prices_and_generic_bot_quota(db: Session) -> None:
    """平台保存套餐时必须同时维护通用机器人额度和公开价格字段。"""

    plan = platform_admin.save_plan(
        db,
        code="commercial-standard",
        name="商业标准版",
        provider_node_limit=2,
        bot_limit=3,
        monthly_price_cents=25_900,
        yearly_price_cents=259_000,
        trial_days=0,
        is_public=True,
        is_featured=True,
        display_order=20,
        status="active",
    )

    assert plan.provider_node_limit == 2
    assert plan.bot_limit == 3
    assert plan.monthly_price_cents == 25_900
    assert plan.yearly_price_cents == 259_000
    assert plan.is_public is True
    assert plan.is_featured is True
    assert plan.display_order == 20


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"bot_limit": -1}, "bot_limit_invalid"),
        ({"monthly_price_cents": -1}, "monthly_price_invalid"),
        ({"yearly_price_cents": -1}, "yearly_price_invalid"),
        ({"trial_days": -1}, "trial_days_invalid"),
        ({"trial_days": 366}, "trial_days_invalid"),
        ({"display_order": -1}, "display_order_invalid"),
        ({"bot_limit": 0, "is_public": True}, "public_plan_bot_limit_invalid"),
    ],
)
def test_save_plan_rejects_invalid_commercial_values(
    db: Session,
    overrides: dict[str, object],
    error_code: str,
) -> None:
    """负数金额、负额度、异常试用天数和零额度公开套餐不得写入数据库。"""

    values: dict[str, object] = {
        "code": f"invalid-{error_code.replace('_', '-')}",
        "name": "非法套餐",
        "provider_node_limit": 1,
        "bot_limit": 1,
        "monthly_price_cents": 9_900,
        "yearly_price_cents": 99_000,
        "trial_days": 0,
        "is_public": False,
        "is_featured": False,
        "display_order": 10,
        "status": "active",
    }
    values.update(overrides)

    with pytest.raises(PlatformAdminError, match=error_code):
        platform_admin.save_plan(db, **values)


def test_platform_admin_can_renew_disable_and_reset_admin_password(db: Session) -> None:
    """平台能够完成租户生命周期的三个高频维护动作。"""

    first_plan = _create_plan(db)
    second_plan = SaasPlan(code="upgrade-plan", name="升级套餐", provider_node_limit=5, status="active")
    db.add(second_plan)
    db.commit()
    created = platform_admin.create_tenant_with_subscription(
        db,
        code="lifecycle",
        name="生命周期租户",
        plan_id=first_plan.id,
        expires_at=now_shanghai() + timedelta(days=30),
        admin_username="root",
        admin_display_name="租户管理员",
    )

    renewed = platform_admin.renew_tenant_subscription(
        db,
        tenant_id=created.tenant_id,
        plan_id=second_plan.id,
        expires_at=now_shanghai() + timedelta(days=365),
    )
    tenant = platform_admin.set_tenant_status(db, tenant_id=created.tenant_id, status="disabled")
    reset_password = platform_admin.reset_tenant_admin_password(
        db,
        tenant_id=created.tenant_id,
        user_id=created.admin_user_id,
    )

    admin = db.get(TenantUser, created.admin_user_id)
    assert renewed.plan_id == second_plan.id
    assert tenant.status == "disabled"
    assert admin.auth_version == 2
    assert admin.must_change_password is True
    assert verify_password(reset_password, admin.password_hash)


def test_platform_web_creates_tenant_and_only_shows_password_on_success_response(tmp_path, monkeypatch) -> None:
    """平台页面必须校验平台 CSRF，并且初始密码不能通过跳转 URL 持久传播。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'platform-web.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(platform_admin_web, "SessionLocal", factory)

    with factory() as db:
        platform_user = PlatformUser(
            username="platform-admin",
            display_name="平台管理员",
            role="platform_admin",
            password_hash=hash_password("Platform-secret-123"),
            is_enabled=True,
            must_change_password=False,
        )
        plan = SaasPlan(code="web-plan", name="页面套餐", provider_node_limit=2, status="active")
        db.add_all([platform_user, plan])
        db.commit()
        db.refresh(platform_user)
        db.refresh(plan)
        created_session = create_platform_session(db, platform_user)
        plan_id = int(plan.id)

    test_app = FastAPI()

    @test_app.middleware("http")
    async def restore_platform_principal(request: Request, call_next):
        # 独立路由测试只复刻平台身份恢复，不启动主应用 Worker 或安装流程。
        with factory() as db:
            request.state.platform_principal = load_platform_principal(
                db,
                request.cookies.get(PLATFORM_SESSION_COOKIE),
            )
        return await call_next(request)

    test_app.include_router(platform_admin_web.router)
    with TestClient(test_app) as client:
        client.cookies.set(PLATFORM_SESSION_COOKIE, created_session.raw_token)
        client.cookies.set(PLATFORM_CSRF_COOKIE, created_session.raw_csrf_token)
        response = client.post(
            "/platform/tenants",
            data={
                "csrf_token": created_session.raw_csrf_token,
                "code": "web-tenant",
                "name": "页面创建租户",
                "plan_id": str(plan_id),
                "expires_on": "2027-08-01",
                "admin_username": "admin",
                "admin_display_name": "企业管理员",
            },
        )

        assert response.status_code == 201
        password_match = re.search(r"一次性初始密码.*?<code>([^<]+)</code>", response.text, re.S)
        assert password_match is not None
        initial_password = password_match.group(1)

        with factory() as db:
            tenant = db.scalar(select(Tenant).where(Tenant.code == "web-tenant"))
            admin = db.scalar(select(TenantUser).where(TenantUser.tenant_id == tenant.id))
            assert verify_password(initial_password, admin.password_hash)
            tenant_id = int(tenant.id)

        refreshed = client.get(f"/platform/tenants/{tenant_id}")
        assert refreshed.status_code == 200
        assert initial_password not in refreshed.text

        rejected = client.post(
            "/platform/plans",
            data={
                "csrf_token": "forged",
                "code": "forged-plan",
                "name": "伪造套餐",
                "provider_node_limit": "1",
            },
        )
        assert rejected.status_code == 403

        saved_plan = client.post(
            "/platform/plans",
            data={
                "csrf_token": created_session.raw_csrf_token,
                "code": "priced-plan",
                "name": "精确价格套餐",
                "bot_limit": "3",
                "monthly_price": "259.90",
                "yearly_price": "2599.00",
                "trial_days": "0",
                "is_public": "true",
                "is_featured": "true",
                "display_order": "20",
                "provider_node_limit": "2",
                "status": "active",
            },
            follow_redirects=False,
        )
        assert saved_plan.status_code == 303

        with factory() as db:
            priced_plan = db.scalar(select(SaasPlan).where(SaasPlan.code == "priced-plan"))
            assert priced_plan.monthly_price_cents == 25_990
            assert priced_plan.yearly_price_cents == 259_900
            assert priced_plan.bot_limit == 3

        plans_page = client.get("/platform/plans")
        assert plans_page.status_code == 200
        assert "¥259.90" in plans_page.text
        assert "机器人额度" in plans_page.text

    Base.metadata.drop_all(bind=engine)
    engine.dispose()
