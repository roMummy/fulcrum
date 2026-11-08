from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.time import now_shanghai
from app.models.saas import BillingOrder, SaasPlan, Tenant, TenantSubscription
from app.services.billing import BillingError, auto_fulfill_paid_order


@pytest.fixture()
def payment_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        tenant = Tenant(code="payment-hook-test", name="支付回调测试租户", status="active")
        plan = SaasPlan(code="payment-hook-plan", name="支付回调套餐", status="active", bot_limit=1)
        db.add_all([tenant, plan])
        db.flush()
        order = BillingOrder(
            order_no="YS20261108PAYMENT001",
            tenant_id=tenant.id,
            plan_id=plan.id,
            billing_cycle="monthly",
            bot_limit_snapshot=1,
            amount_cents=9900,
            status="pending_payment",
        )
        db.add(order)
        db.commit()
        yield db, order.order_no
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_paid_callback_auto_fulfills_and_is_idempotent(payment_db) -> None:
    db, order_no = payment_db
    first = auto_fulfill_paid_order(
        db,
        order_no=order_no,
        amount_cents=9900,
        provider_transaction_id="TX-PAY-001",
    )
    second = auto_fulfill_paid_order(
        db,
        order_no=order_no,
        amount_cents=9900,
        provider_transaction_id="TX-PAY-001",
    )
    assert first.id == second.id
    assert second.status == "approved"
    assert second.fulfilled_subscription_id is not None
    subscriptions = db.scalars(select(TenantSubscription).where(TenantSubscription.tenant_id == first.tenant_id)).all()
    assert len(subscriptions) == 1


def test_paid_callback_rejects_amount_mismatch(payment_db) -> None:
    db, order_no = payment_db
    with pytest.raises(BillingError, match="payment_amount_mismatch"):
        auto_fulfill_paid_order(
            db,
            order_no=order_no,
            amount_cents=1,
            provider_transaction_id="TX-PAY-BAD",
        )
