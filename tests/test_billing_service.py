from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.settings import settings
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import BillingOrder, PlatformAuditLog, PlatformUser, SaasPlan, Tenant, TenantSubscription
from app.services.billing import (
    BillingError,
    approve_order,
    cancel_order,
    create_billing_order,
    get_tenant_order,
    reject_order,
    submit_payment_evidence,
)
from app.services.payment_gateways.manual import ManualPaymentGateway
from app.services.saas_auth import hash_password


@pytest.fixture()
def db(tmp_path):
    """订单服务测试使用隔离 SQLite 数据库，避免审核流程写入开发订阅。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'billing-service.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_billing(db: Session) -> tuple[Tenant, Tenant, SaasPlan, PlatformUser]:
    """创建两个租户、公开套餐和平台审核账号。"""

    first = Tenant(code="billing-a", name="计费企业甲", status="active")
    second = Tenant(code="billing-b", name="计费企业乙", status="active")
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
        username="billing-reviewer",
        display_name="订单审核员",
        password_hash=hash_password("Reviewer-secret-123"),
        role="platform_admin",
        must_change_password=False,
    )
    db.add_all([first, second, plan, reviewer])
    db.commit()
    for record in (first, second, plan, reviewer):
        db.refresh(record)
    return first, second, plan, reviewer


def test_order_amount_and_quota_are_calculated_from_plan(db: Session) -> None:
    """客户端只选择套餐和周期，订单金额与额度快照必须由服务端套餐决定。"""

    tenant, _second, plan, _reviewer = _seed_billing(db)
    monthly = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle="monthly")
    yearly = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle="yearly")

    assert monthly.amount_cents == 25_900
    assert yearly.amount_cents == 259_000
    assert monthly.bot_limit_snapshot == 3
    assert monthly.order_no != yearly.order_no


def test_duplicate_unfinished_order_is_reused(db: Session) -> None:
    """同一租户、套餐和周期的未完成订单应复用，避免重复上传与审核。"""

    tenant, _second, plan, _reviewer = _seed_billing(db)
    first = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle="monthly")
    second = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle="monthly")

    assert first.id == second.id
    assert db.scalar(select(func.count(BillingOrder.id))) == 1


def test_payment_submission_and_rejection_resubmission_follow_state_machine(db: Session) -> None:
    """付款凭证只允许从待付款或已驳回进入待审核，批准和取消后禁止再次提交。"""

    tenant, _second, plan, reviewer = _seed_billing(db)
    order = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle="monthly")
    submitted = submit_payment_evidence(
        db,
        tenant_id=tenant.id,
        order_id=order.id,
        payment_reference="TXN-001",
        payment_proof_path="tenant_billing/1/order/proof.png",
    )
    assert submitted.status == "payment_submitted"

    rejected = reject_order(db, order_id=order.id, reviewer_user_id=reviewer.id, reason="流水号无法核对")
    assert rejected.status == "rejected"
    resubmitted = submit_payment_evidence(
        db,
        tenant_id=tenant.id,
        order_id=order.id,
        payment_reference="TXN-002",
        payment_proof_path="tenant_billing/1/order/proof.pdf",
    )
    assert resubmitted.status == "payment_submitted"
    approved = approve_order(db, order_id=order.id, reviewer_user_id=reviewer.id)
    assert approved.status == "approved"

    with pytest.raises(BillingError, match="order_payment_not_editable"):
        submit_payment_evidence(
            db,
            tenant_id=tenant.id,
            order_id=order.id,
            payment_reference="TXN-003",
            payment_proof_path="tenant_billing/1/order/proof.webp",
        )

    cancelled_order = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle="yearly")
    cancel_order(db, tenant_id=tenant.id, order_id=cancelled_order.id)
    with pytest.raises(BillingError, match="order_payment_not_editable"):
        submit_payment_evidence(
            db,
            tenant_id=tenant.id,
            order_id=cancelled_order.id,
            payment_reference="TXN-004",
            payment_proof_path="tenant_billing/1/order/proof.png",
        )


def test_tenant_order_lookup_cannot_cross_tenant_boundary(db: Session) -> None:
    """订单查询必须同时使用可信 tenant_id，不能只凭可枚举主键读取其他企业订单。"""

    first, second, plan, _reviewer = _seed_billing(db)
    order = create_billing_order(db, tenant_id=first.id, plan_id=plan.id, billing_cycle="monthly")

    with pytest.raises(BillingError, match="order_not_found"):
        get_tenant_order(db, tenant_id=second.id, order_id=order.id)


def test_approve_order_is_idempotent_and_fulfils_one_subscription(db: Session) -> None:
    """重复批准只能履约一次，并从旧订阅到期时间继续延长一个完整周期。"""

    tenant, _second, plan, reviewer = _seed_billing(db)
    trial_plan = SaasPlan(
        code="trial",
        name="免费试用",
        provider_node_limit=1,
        bot_limit=1,
        trial_days=7,
        status="active",
    )
    db.add(trial_plan)
    db.flush()
    trial_expiry = now_shanghai() + timedelta(days=5)
    db.add(
        TenantSubscription(
            tenant_id=tenant.id,
            plan_id=trial_plan.id,
            status="active",
            starts_at=now_shanghai() - timedelta(days=2),
            expires_at=trial_expiry,
        )
    )
    db.commit()
    order = create_billing_order(db, tenant_id=tenant.id, plan_id=plan.id, billing_cycle="monthly")
    submit_payment_evidence(
        db,
        tenant_id=tenant.id,
        order_id=order.id,
        payment_reference="TXN-IDEMPOTENT",
        payment_proof_path="tenant_billing/1/order/proof.png",
    )

    first_result = approve_order(db, order_id=order.id, reviewer_user_id=reviewer.id)
    second_result = approve_order(db, order_id=order.id, reviewer_user_id=reviewer.id)
    subscriptions = db.scalars(
        select(TenantSubscription).where(TenantSubscription.tenant_id == tenant.id)
    ).all()
    fulfilled = db.get(TenantSubscription, first_result.fulfilled_subscription_id)

    assert first_result.id == second_result.id
    assert len(subscriptions) == 2
    assert len([item for item in subscriptions if item.status == "active"]) == 1
    assert fulfilled.plan_id == plan.id
    assert ensure_shanghai_datetime(fulfilled.expires_at) > ensure_shanghai_datetime(trial_expiry) + timedelta(days=27)
    assert db.scalar(select(func.count(PlatformAuditLog.id)).where(PlatformAuditLog.action == "billing_order.approve")) == 1


def test_manual_payment_gateway_reads_configured_instructions(monkeypatch) -> None:
    """人工付款网关只返回配置化说明，不发起外部网络请求。"""

    monkeypatch.setattr(settings, "billing_payment_title", "Fulcrum 收款")
    monkeypatch.setattr(settings, "billing_payment_instructions", "付款后上传凭证")
    monkeypatch.setattr(settings, "billing_payment_qr_image", "/static/images/payment.png")
    monkeypatch.setattr(settings, "billing_payment_bank_details", "测试银行账户")

    instructions = ManualPaymentGateway().get_instructions()

    assert instructions.title == "Fulcrum 收款"
    assert instructions.instructions == "付款后上传凭证"
    assert instructions.qr_image == "/static/images/payment.png"
    assert instructions.bank_details == "测试银行账户"
