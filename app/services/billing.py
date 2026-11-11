"""Fulcrum 套餐订单、人工付款和订阅履约服务。"""

from __future__ import annotations

import calendar
import secrets
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import case, select
from sqlalchemy.orm import Session, selectinload

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import (
    BILLING_CYCLES,
    BillingOrder,
    PlatformAuditLog,
    PlatformUser,
    SaasPlan,
    Tenant,
    TenantSubscription,
)


class BillingError(ValueError):
    """订单输入、状态或订阅履约不满足业务规则。"""


def list_public_plans(db: Session) -> list[SaasPlan]:
    """返回启用且公开的通用机器人套餐。"""

    return db.scalars(
        select(SaasPlan)
        .where(SaasPlan.status == "active", SaasPlan.is_public.is_(True))
        .order_by(SaasPlan.display_order.asc(), SaasPlan.id.asc())
    ).all()


def calculate_plan_amount(plan: SaasPlan, billing_cycle: str) -> int:
    """根据数据库套餐和规范周期计算订单金额，拒绝客户端金额参与。"""

    normalized_cycle = (billing_cycle or "").strip().lower()
    if normalized_cycle not in BILLING_CYCLES:
        raise BillingError("billing_cycle_invalid")
    amount = plan.monthly_price_cents if normalized_cycle == "monthly" else plan.yearly_price_cents
    if int(amount) <= 0:
        raise BillingError("plan_price_invalid")
    return int(amount)


def _new_order_no() -> str:
    """生成高熵、可人工核对且不包含租户信息的订单号。"""

    return f"YS{now_shanghai():%Y%m%d}{secrets.token_hex(10).upper()}"


def create_billing_order(
    db: Session,
    *,
    tenant_id: int,
    plan_id: int,
    billing_cycle: str,
) -> BillingOrder:
    """按服务端套餐快照创建订单，并复用同套餐同周期未完成订单。"""

    normalized_cycle = (billing_cycle or "").strip().lower()
    tenant = db.get(Tenant, int(tenant_id))
    if tenant is None:
        raise BillingError("tenant_not_found")
    if tenant.status != "active":
        raise BillingError("tenant_disabled")
    plan = db.get(SaasPlan, int(plan_id))
    if plan is None or plan.status != "active" or not plan.is_public:
        raise BillingError("plan_not_available")
    if plan.bot_limit <= 0:
        raise BillingError("plan_bot_limit_invalid")
    amount_cents = calculate_plan_amount(plan, normalized_cycle)

    existing = db.scalar(
        select(BillingOrder)
        .where(
            BillingOrder.tenant_id == tenant.id,
            BillingOrder.plan_id == plan.id,
            BillingOrder.billing_cycle == normalized_cycle,
            BillingOrder.status.in_(("pending_payment", "payment_submitted", "rejected")),
        )
        .order_by(BillingOrder.id.desc())
        .limit(1)
    )
    if existing is not None:
        return existing

    order = BillingOrder(
        order_no=_new_order_no(),
        tenant_id=tenant.id,
        plan_id=plan.id,
        billing_cycle=normalized_cycle,
        bot_limit_snapshot=plan.bot_limit,
        amount_cents=amount_cents,
        status="pending_payment",
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def get_tenant_order(db: Session, *, tenant_id: int, order_id: int) -> BillingOrder:
    """按可信租户范围读取订单；其他租户和不存在订单统一返回未找到。"""

    order = db.scalar(
        select(BillingOrder).where(
            BillingOrder.id == int(order_id),
            BillingOrder.tenant_id == int(tenant_id),
        )
    )
    if order is None:
        raise BillingError("order_not_found")
    return order


def get_billing_order(db: Session, *, order_id: int, for_update: bool = False) -> BillingOrder:
    """供平台审核读取订单，可选行锁用于保护状态检查和订阅履约。"""

    query = (
        select(BillingOrder)
        .where(BillingOrder.id == int(order_id))
        .options(
            selectinload(BillingOrder.tenant),
            selectinload(BillingOrder.plan),
            selectinload(BillingOrder.reviewed_by_user),
            selectinload(BillingOrder.fulfilled_subscription),
        )
    )
    if for_update:
        query = query.with_for_update()
    order = db.scalar(query)
    if order is None:
        raise BillingError("order_not_found")
    return order


def list_billing_orders(db: Session) -> list[BillingOrder]:
    """返回平台订单清单，待审核状态优先，其余按最新创建时间排列。"""

    status_priority = case(
        (BillingOrder.status == "payment_submitted", 0),
        (BillingOrder.status == "pending_payment", 1),
        (BillingOrder.status == "rejected", 2),
        else_=3,
    )
    return db.scalars(
        select(BillingOrder)
        .options(selectinload(BillingOrder.tenant), selectinload(BillingOrder.plan))
        .order_by(status_priority.asc(), BillingOrder.id.desc())
    ).all()


def list_tenant_orders(db: Session, *, tenant_id: int) -> list[BillingOrder]:
    """只返回当前可信租户的订单历史。"""

    return db.scalars(
        select(BillingOrder)
        .where(BillingOrder.tenant_id == int(tenant_id))
        .order_by(BillingOrder.id.desc())
    ).all()


def get_current_subscription(db: Session, *, tenant_id: int) -> TenantSubscription | None:
    """返回租户最新订阅及套餐，用于账户页展示和后续额度判断。"""

    return db.scalar(
        select(TenantSubscription)
        .where(TenantSubscription.tenant_id == int(tenant_id))
        .options(
            selectinload(TenantSubscription.plan),
            selectinload(TenantSubscription.fulfilled_order),
        )
        .order_by(TenantSubscription.starts_at.desc(), TenantSubscription.id.desc())
        .limit(1)
    )


def subscription_is_active(subscription: TenantSubscription | None, *, now: datetime | None = None) -> bool:
    """统一判断订阅是否仍可使用开放业务能力。

    数据库中的 ``status`` 由履约流程维护，但到期时间必须在每次业务请求时
    再校验一次，避免定时任务尚未把状态改成 ``expired`` 时继续放行请求。
    """

    if subscription is None or subscription.status != "active":
        return False
    current = ensure_shanghai_datetime(now) if now is not None else now_shanghai()
    return subscription.expires_at is None or ensure_shanghai_datetime(subscription.expires_at) > current


def _normalize_proof_path(value: str) -> str:
    """只接受私有存储相对路径，阻止订单服务持久化目录穿越路径。"""

    normalized = (value or "").strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise BillingError("payment_proof_path_invalid")
    return str(path)


def submit_payment_evidence(
    db: Session,
    *,
    tenant_id: int,
    order_id: int,
    payment_reference: str,
    payment_proof_path: str,
) -> BillingOrder:
    """保存人工付款索引信息，并把订单推进到等待平台审核。"""

    order = get_tenant_order(db, tenant_id=tenant_id, order_id=order_id)
    if order.status not in {"pending_payment", "rejected"}:
        raise BillingError("order_payment_not_editable")
    normalized_reference = (payment_reference or "").strip()
    if not normalized_reference or len(normalized_reference) > 255:
        raise BillingError("payment_reference_invalid")
    order.payment_reference = normalized_reference
    order.payment_proof_path = _normalize_proof_path(payment_proof_path)
    order.payment_submitted_at = now_shanghai()
    order.status = "payment_submitted"
    # 驳回后重新提交时清理上一次审核结论，历史原因仍保留在平台审计表。
    order.reviewed_by_user_id = None
    order.reviewed_at = None
    order.rejection_reason = None
    db.commit()
    db.refresh(order)
    return order


def cancel_order(db: Session, *, tenant_id: int, order_id: int) -> BillingOrder:
    """取消尚未进入审核或履约的租户订单。"""

    order = get_tenant_order(db, tenant_id=tenant_id, order_id=order_id)
    if order.status == "cancelled":
        return order
    if order.status not in {"pending_payment", "rejected"}:
        raise BillingError("order_cannot_cancel")
    order.status = "cancelled"
    db.commit()
    db.refresh(order)
    return order


def _add_months(value: datetime, months: int) -> datetime:
    """按自然月延长订阅，并把月末日期收敛到目标月份有效日期。"""

    normalized = ensure_shanghai_datetime(value)
    month_index = normalized.month - 1 + months
    year = normalized.year + month_index // 12
    month = month_index % 12 + 1
    day = min(normalized.day, calendar.monthrange(year, month)[1])
    return normalized.replace(year=year, month=month, day=day)


def _require_reviewer(db: Session, reviewer_user_id: int) -> PlatformUser:
    """审核人必须来自启用的平台管理员账号，不能由租户用户主键冒充。"""

    reviewer = db.get(PlatformUser, int(reviewer_user_id))
    if reviewer is None or reviewer.role != "platform_admin" or not reviewer.is_enabled:
        raise BillingError("reviewer_not_allowed")
    return reviewer


def approve_order(db: Session, *, order_id: int, reviewer_user_id: int) -> BillingOrder:
    """幂等批准订单，并在同一事务内创建唯一履约订阅和平台审计。"""

    try:
        order = get_billing_order(db, order_id=order_id, for_update=True)
        if order.status == "approved" and order.fulfilled_subscription_id is not None:
            return order
        if order.status != "payment_submitted":
            raise BillingError("order_not_approvable")
        reviewer = _require_reviewer(db, reviewer_user_id)
        current_time = now_shanghai()
        active_subscriptions = db.scalars(
            select(TenantSubscription)
            .where(
                TenantSubscription.tenant_id == order.tenant_id,
                TenantSubscription.status == "active",
            )
            .with_for_update()
        ).all()
        renewal_base = current_time
        for subscription in active_subscriptions:
            if subscription.expires_at is not None:
                candidate = ensure_shanghai_datetime(subscription.expires_at)
                if candidate > renewal_base:
                    renewal_base = candidate
            subscription.status = "expired"

        duration_months = 1 if order.billing_cycle == "monthly" else 12
        fulfilled = TenantSubscription(
            tenant_id=order.tenant_id,
            plan_id=order.plan_id,
            status="active",
            starts_at=current_time,
            expires_at=_add_months(renewal_base, duration_months),
            notes=f"billing_order:{order.order_no}",
        )
        db.add(fulfilled)
        db.flush()
        order.status = "approved"
        order.reviewed_by_user_id = reviewer.id
        order.reviewed_at = current_time
        order.rejection_reason = None
        order.fulfilled_subscription_id = fulfilled.id
        db.add(
            PlatformAuditLog(
                actor_user_id=reviewer.id,
                action="billing_order.approve",
                target_type="billing_order",
                target_id=order.id,
                target_tenant_id=order.tenant_id,
                # 审计记录订单号和履约主键，不记录付款凭证路径、内容或设备信息。
                detail=f"order_no={order.order_no};subscription_id={fulfilled.id};plan_id={order.plan_id}",
            )
        )
        db.commit()
        db.refresh(order)
        return order
    except BaseException:
        db.rollback()
        raise


def auto_fulfill_paid_order(
    db: Session,
    *,
    order_no: str,
    amount_cents: int,
    provider_transaction_id: str,
    paid_at: datetime | None = None,
) -> BillingOrder:
    """校验支付回调金额后自动履约订单，重复通知返回原订单。

    该入口不模拟平台页面，也不需要平台人工账号；支付渠道已经完成签名校验后，
    这里在同一事务中完成订单状态、订阅有效期和审计记录的变更。
    """

    normalized_order_no = str(order_no or "").strip()
    normalized_transaction_id = str(provider_transaction_id or "").strip()
    if not normalized_order_no or not normalized_transaction_id:
        raise BillingError("payment_callback_fields_invalid")
    try:
        order = db.scalar(
            select(BillingOrder)
            .where(BillingOrder.order_no == normalized_order_no)
            .options(selectinload(BillingOrder.plan))
            .with_for_update()
        )
        if order is None:
            raise BillingError("order_not_found")
        if order.status == "approved" and order.fulfilled_subscription_id is not None:
            return order
        if int(order.amount_cents) != int(amount_cents):
            raise BillingError("payment_amount_mismatch")
        if order.status not in {"pending_payment", "payment_submitted", "rejected"}:
            raise BillingError("order_not_fulfillable")
        current_time = ensure_shanghai_datetime(paid_at) if paid_at is not None else now_shanghai()
        active_subscriptions = db.scalars(
            select(TenantSubscription)
            .where(
                TenantSubscription.tenant_id == order.tenant_id,
                TenantSubscription.status == "active",
            )
            .with_for_update()
        ).all()
        renewal_base = current_time
        for subscription in active_subscriptions:
            if subscription.expires_at is not None:
                candidate = ensure_shanghai_datetime(subscription.expires_at)
                if candidate > renewal_base:
                    renewal_base = candidate
            subscription.status = "expired"
        duration_months = 1 if order.billing_cycle == "monthly" else 12
        fulfilled = TenantSubscription(
            tenant_id=order.tenant_id,
            plan_id=order.plan_id,
            status="active",
            starts_at=current_time,
            expires_at=_add_months(renewal_base, duration_months),
            notes=f"payment:{normalized_transaction_id};billing_order:{order.order_no}",
        )
        db.add(fulfilled)
        db.flush()
        order.status = "approved"
        order.payment_reference = normalized_transaction_id
        order.payment_submitted_at = current_time
        order.reviewed_at = current_time
        order.rejection_reason = None
        order.fulfilled_subscription_id = fulfilled.id
        db.add(
            PlatformAuditLog(
                actor_user_id=None,
                action="billing_order.auto_fulfill",
                target_type="billing_order",
                target_id=order.id,
                target_tenant_id=order.tenant_id,
                detail=f"order_no={order.order_no};subscription_id={fulfilled.id};provider_transaction_id={normalized_transaction_id}",
            )
        )
        db.commit()
        db.refresh(order)
        return order
    except BaseException:
        db.rollback()
        raise


def reject_order(
    db: Session,
    *,
    order_id: int,
    reviewer_user_id: int,
    reason: str,
) -> BillingOrder:
    """驳回待审核付款凭证，并保留可重新提交的订单。"""

    try:
        order = get_billing_order(db, order_id=order_id, for_update=True)
        if order.status != "payment_submitted":
            raise BillingError("order_not_rejectable")
        reviewer = _require_reviewer(db, reviewer_user_id)
        normalized_reason = (reason or "").strip()
        if not normalized_reason:
            raise BillingError("rejection_reason_required")
        if len(normalized_reason) > 1000:
            raise BillingError("rejection_reason_too_long")
        current_time = now_shanghai()
        order.status = "rejected"
        order.reviewed_by_user_id = reviewer.id
        order.reviewed_at = current_time
        order.rejection_reason = normalized_reason
        db.add(
            PlatformAuditLog(
                actor_user_id=reviewer.id,
                action="billing_order.reject",
                target_type="billing_order",
                target_id=order.id,
                target_tenant_id=order.tenant_id,
                detail=f"order_no={order.order_no};reason={normalized_reason}",
            )
        )
        db.commit()
        db.refresh(order)
        return order
    except BaseException:
        db.rollback()
        raise
