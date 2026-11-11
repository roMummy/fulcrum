from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.services.registration as registration
from app.core.db import Base
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import PlatformAuditLog, SaasPlan, Tenant, TenantSubscription, TenantUser
from app.services.saas_auth import authenticate_tenant_account


@pytest.fixture()
def db(tmp_path):
    """公开注册测试使用隔离 SQLite 数据库，确保失败事务不会污染开发数据。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'public-registration.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _create_trial_plan(db: Session, *, status: str = "active") -> SaasPlan:
    """创建注册服务所需的七天试用套餐。"""

    plan = SaasPlan(
        code="trial",
        name="免费试用",
        provider_node_limit=1,
        bot_limit=1,
        monthly_price_cents=0,
        yearly_price_cents=0,
        trial_days=7,
        is_public=False,
        status=status,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def test_register_trial_tenant_creates_owner_and_subscription_atomically(db: Session) -> None:
    """注册成功后企业、所有者、试用订阅和审计必须同时存在。"""

    plan = _create_trial_plan(db)
    before = now_shanghai()
    created = registration.register_trial_tenant(
        db,
        mobile="+86 138-0013-8000",
        password="Owner-secret-123",
        enterprise_name="Fulcrum 演示企业",
        contact_name="刘先生",
    )

    tenant = db.get(Tenant, created.tenant_id)
    owner = db.get(TenantUser, created.owner_user_id)
    subscription = db.get(TenantSubscription, created.subscription_id)
    audit = db.scalar(select(PlatformAuditLog).where(PlatformAuditLog.target_tenant_id == tenant.id))

    assert tenant.code.startswith("org-")
    assert "Fulcrum" not in tenant.code
    assert tenant.contact_phone == "13800138000"
    assert owner.mobile == "13800138000"
    assert owner.is_owner is True
    assert owner.role == "tenant_admin"
    assert owner.must_change_password is False
    assert subscription.plan_id == plan.id
    assert subscription.status == "active"
    assert ensure_shanghai_datetime(subscription.expires_at) == ensure_shanghai_datetime(
        subscription.starts_at
    ) + timedelta(days=7)
    assert ensure_shanghai_datetime(subscription.starts_at) >= before
    assert "13800138000" not in (audit.detail or "")

    assert authenticate_tenant_account(db, "13800138000", "Owner-secret-123").id == owner.id


def test_normalized_mobile_is_globally_unique(db: Session) -> None:
    """同一手机号使用不同空格、短横线或国家码写法也只能注册一个企业。"""

    _create_trial_plan(db)
    registration.register_trial_tenant(
        db,
        mobile="138 0013 8000",
        password="Owner-secret-123",
        enterprise_name="首个企业",
        contact_name="所有者甲",
    )

    with pytest.raises(registration.RegistrationError, match="mobile_already_registered"):
        registration.register_trial_tenant(
            db,
            mobile="+86-13800138000",
            password="Owner-secret-456",
            enterprise_name="第二个企业",
            contact_name="所有者乙",
        )


def test_registration_rolls_back_when_password_hash_fails(db: Session, monkeypatch) -> None:
    """密码哈希失败时不得留下孤立企业、所有者、订阅或审计记录。"""

    _create_trial_plan(db)

    def fail_hash(_password: str) -> str:
        raise RuntimeError("password_hash_failed")

    monkeypatch.setattr(registration, "hash_password", fail_hash)
    with pytest.raises(RuntimeError, match="password_hash_failed"):
        registration.register_trial_tenant(
            db,
            mobile="13900139000",
            password="Owner-secret-123",
            enterprise_name="应回滚企业",
            contact_name="失败用户",
        )

    assert db.scalar(select(func.count(Tenant.id))) == 0
    assert db.scalar(select(func.count(TenantUser.id))) == 0
    assert db.scalar(select(func.count(TenantSubscription.id))) == 0
    assert db.scalar(select(func.count(PlatformAuditLog.id))) == 0


@pytest.mark.parametrize(("create_plan", "status", "error_code"), [(False, "active", "trial_plan_not_found"), (True, "disabled", "trial_plan_disabled")])
def test_registration_rejects_missing_or_disabled_trial_plan(
    db: Session,
    create_plan: bool,
    status: str,
    error_code: str,
) -> None:
    """平台未准备好有效试用套餐时必须拒绝产生不完整注册。"""

    if create_plan:
        _create_trial_plan(db, status=status)

    with pytest.raises(registration.RegistrationError, match=error_code):
        registration.register_trial_tenant(
            db,
            mobile="13700137000",
            password="Owner-secret-123",
            enterprise_name="无套餐企业",
            contact_name="所有者",
        )
