from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import Base
from app.models.saas import (
    BillingOrder,
    BotBinding,
    DeviceTrialClaim,
    SaasPlan,
    Tenant,
    TenantUser,
)


@pytest.fixture()
def db(tmp_path):
    """为商业化模型约束创建独立 SQLite 数据库，避免测试写入开发环境。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'public-billing-models.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _create_tenant(db: Session, code: str) -> Tenant:
    """创建最小租户，供手机号、订单和机器人绑定约束测试复用。"""

    tenant = Tenant(code=code, name=f"{code} 企业", status="active")
    db.add(tenant)
    db.flush()
    return tenant


def test_public_billing_models_are_registered() -> None:
    """公开计费和防滥用模型必须进入统一 SQLAlchemy 元数据。"""

    assert {
        "billing_orders",
        "bot_bindings",
        "device_trial_claims",
    }.issubset(Base.metadata.tables)


def test_plan_exposes_generic_bot_quota_and_prices(db: Session) -> None:
    """套餐同时保留旧节点额度和新的通用机器人额度，迁移期不能互相覆盖。"""

    plan = SaasPlan(
        code="starter",
        name="入门版",
        provider_node_limit=1,
        bot_limit=1,
        monthly_price_cents=9900,
        yearly_price_cents=99000,
        trial_days=0,
        is_public=True,
        is_featured=False,
        display_order=10,
        status="active",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    assert plan.provider_node_limit == 1
    assert plan.bot_limit == 1
    assert plan.monthly_price_cents == 9900
    assert plan.yearly_price_cents == 99000
    assert plan.is_public is True


def test_owner_mobile_is_globally_unique_across_tenants(db: Session) -> None:
    """一个手机号只能注册一个企业，即使租户不同也不能创建第二个所有者。"""

    first_tenant = _create_tenant(db, "mobile-a")
    second_tenant = _create_tenant(db, "mobile-b")
    db.add(
        TenantUser(
            tenant_id=first_tenant.id,
            username="owner-a",
            mobile="13800138000",
            display_name="企业主甲",
            role="tenant_admin",
            password_hash="hash-a",
            is_owner=True,
        )
    )
    db.commit()

    db.add(
        TenantUser(
            tenant_id=second_tenant.id,
            username="owner-b",
            mobile="13800138000",
            display_name="企业主乙",
            role="tenant_admin",
            password_hash="hash-b",
            is_owner=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_order_number_is_globally_unique(db: Session) -> None:
    """订单号不能因租户不同而重复，避免审核和凭证下载定位到错误订单。"""

    first_tenant = _create_tenant(db, "order-a")
    second_tenant = _create_tenant(db, "order-b")
    plan = SaasPlan(
        code="order-plan",
        name="订单套餐",
        provider_node_limit=1,
        bot_limit=1,
        monthly_price_cents=9900,
        yearly_price_cents=99000,
        status="active",
    )
    db.add(plan)
    db.flush()
    db.add_all(
        [
            BillingOrder(
                order_no="ord_unique_001",
                tenant_id=first_tenant.id,
                plan_id=plan.id,
                billing_cycle="monthly",
                bot_limit_snapshot=1,
                amount_cents=9900,
                status="pending_payment",
            ),
            BillingOrder(
                order_no="ord_unique_001",
                tenant_id=second_tenant.id,
                plan_id=plan.id,
                billing_cycle="monthly",
                bot_limit_snapshot=1,
                amount_cents=9900,
                status="pending_payment",
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_bot_account_binding_is_unique_across_tenants(db: Session) -> None:
    """同一适配类型下的机器人账号只能有一条绑定，不能跨企业重复占用试用额度。"""

    first_tenant = _create_tenant(db, "binding-a")
    second_tenant = _create_tenant(db, "binding-b")
    db.add(
        BotBinding(
            tenant_id=first_tenant.id,
            adapter_type="workwechat",
            bot_account_id="bot-account-001",
            device_fingerprint_hash="a" * 64,
            status="active",
        )
    )
    db.commit()

    db.add(
        BotBinding(
            tenant_id=second_tenant.id,
            adapter_type="workwechat",
            bot_account_id="bot-account-001",
            device_fingerprint_hash="b" * 64,
            status="active",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_device_trial_claim_hash_is_globally_unique(db: Session) -> None:
    """设备试用记录永久全局唯一，解绑或换企业也不能再次领取免费试用。"""

    first_tenant = _create_tenant(db, "trial-a")
    second_tenant = _create_tenant(db, "trial-b")
    fingerprint_hash = "c" * 64
    db.add(DeviceTrialClaim(device_fingerprint_hash=fingerprint_hash, tenant_id=first_tenant.id))
    db.commit()

    db.add(DeviceTrialClaim(device_fingerprint_hash=fingerprint_hash, tenant_id=second_tenant.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
