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


def _create_trial_plan(db: Session, *, status: str = "active", code: str = "trial", trial_days: int = 7, bot_limit: int = 1, monthly_price_cents: int = 0) -> SaasPlan:
    """创建注册服务所需的试用套餐(默认七天、免费;编码可任意)。"""

    plan = SaasPlan(
        code=code,
        name="免费试用",
        provider_node_limit=1,
        bot_limit=bot_limit,
        monthly_price_cents=monthly_price_cents,
        yearly_price_cents=0,
        trial_days=trial_days,
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


def _register(db: Session, mobile: str = "13600136000"):
    return registration.register_trial_tenant(db, mobile=mobile, password="Owner-secret-123", enterprise_name="某企业", contact_name="所有者")


def test_the_trial_plan_is_found_by_its_properties_not_by_the_code_trial(db: Session) -> None:
    """平台管理员自己建的免费试用套餐(编码随意)也要能用于注册:此前只认编码 trial,别的部署会一直提示"暂不可用"。"""

    plan = _create_trial_plan(db, code="free-14d", trial_days=14)
    created = _register(db)
    subscription = db.get(TenantSubscription, created.subscription_id)
    assert subscription.plan_id == plan.id
    assert (subscription.expires_at - subscription.starts_at).days == 14  # 用的是这个套餐自己的试用天数


def test_paid_plans_and_unusable_plans_are_never_picked_for_registration(db: Session) -> None:
    _create_trial_plan(db, code="paid-with-trial", monthly_price_cents=100)  # 收费套餐即使带试用天数也不是免费试用
    _create_trial_plan(db, code="stopped", status="disabled")
    _create_trial_plan(db, code="no-bots", bot_limit=0)
    good = _create_trial_plan(db, code="the-good-one")
    assert db.get(TenantSubscription, _register(db).subscription_id).plan_id == good.id  # 只有最后这个满足全部条件


@pytest.mark.parametrize(
    ("kwargs", "error_code"),
    [({"monthly_price_cents": 100}, "trial_plan_not_found"), ({"trial_days": 0}, "trial_plan_not_found"),
     ({"status": "disabled"}, "trial_plan_disabled"), ({"bot_limit": 0}, "trial_plan_invalid")],
)
def test_each_way_a_trial_plan_can_be_unusable_reports_its_own_reason(db: Session, kwargs: dict, error_code: str) -> None:
    """管理员排查时要能分清:没建 / 被停用 / 机器人数量是 0。"""

    _create_trial_plan(db, **kwargs)
    with pytest.raises(registration.RegistrationError, match=error_code):
        _register(db)
