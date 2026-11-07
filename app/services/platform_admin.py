"""平台管理员维护套餐、租户、订阅和初始管理员的事务服务。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import PlatformAuditLog, SaasPlan, Tenant, TenantSession, TenantSubscription, TenantUser
from app.services.registration import RegistrationError, generate_tenant_code, normalize_mobile
from app.services.saas_auth import generate_initial_password, hash_password


PLAN_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$")


class PlatformAdminError(ValueError):
    """平台管理输入或资源状态不满足业务规则。"""


@dataclass(frozen=True)
class CreatedTenant:
    """创建租户后的最小回执，初始密码只存在于本次函数返回值。"""

    tenant_id: int
    admin_user_id: int
    initial_password: str


def _normalize_plan_code(value: str) -> str:
    """收敛平台套餐编码，租户编码已经由注册服务统一生成。"""

    normalized = (value or "").strip().lower()
    if not PLAN_CODE_PATTERN.fullmatch(normalized):
        raise PlatformAdminError("plan_code_invalid")
    return normalized


def _audit(
    db: Session,
    *,
    actor_user_id: int | None,
    action: str,
    target_type: str,
    target_id: int | None,
    tenant_id: int | None = None,
    detail: str | None = None,
) -> None:
    """记录不含任何原始密码、令牌或激活码的平台审计。"""

    db.add(
        PlatformAuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_tenant_id=tenant_id,
            detail=detail,
        )
    )


def list_plans(db: Session) -> list[SaasPlan]:
    """返回平台套餐清单，启用套餐优先展示。"""

    return db.scalars(select(SaasPlan).order_by(SaasPlan.status.asc(), SaasPlan.id.desc())).all()


def save_plan(
    db: Session,
    *,
    plan_id: int = 0,
    code: str,
    name: str,
    provider_node_limit: int,
    bot_limit: int | None = None,
    monthly_price_cents: int = 0,
    yearly_price_cents: int = 0,
    trial_days: int = 0,
    is_public: bool = False,
    is_featured: bool = False,
    display_order: int = 0,
    status: str = "active",
    description: str = "",
    actor_user_id: int | None = None,
) -> SaasPlan:
    """创建或更新套餐，并把金额严格保存为整数分。"""

    normalized_code = _normalize_plan_code(code)
    normalized_name = (name or "").strip()
    normalized_status = (status or "active").strip().lower()
    if not normalized_name:
        raise PlatformAdminError("plan_name_required")
    if normalized_status not in {"active", "disabled"}:
        raise PlatformAdminError("plan_status_invalid")
    normalized_provider_node_limit = int(provider_node_limit)
    # 旧调用方尚未传 bot_limit 时沿用节点额度，保证平台既有维护动作不会把通用额度清零。
    normalized_bot_limit = normalized_provider_node_limit if bot_limit is None else int(bot_limit)
    normalized_monthly_price = int(monthly_price_cents)
    normalized_yearly_price = int(yearly_price_cents)
    normalized_trial_days = int(trial_days)
    normalized_display_order = int(display_order)
    normalized_is_public = bool(is_public)
    normalized_is_featured = bool(is_featured)
    if normalized_provider_node_limit < 0:
        raise PlatformAdminError("provider_node_limit_invalid")
    if normalized_bot_limit < 0:
        raise PlatformAdminError("bot_limit_invalid")
    if normalized_monthly_price < 0:
        raise PlatformAdminError("monthly_price_invalid")
    if normalized_yearly_price < 0:
        raise PlatformAdminError("yearly_price_invalid")
    # 首版试用只允许 0 到 365 天，防止表单错误产生近乎永久的免费订阅。
    if normalized_trial_days < 0 or normalized_trial_days > 365:
        raise PlatformAdminError("trial_days_invalid")
    if normalized_display_order < 0:
        raise PlatformAdminError("display_order_invalid")
    if normalized_is_public and normalized_bot_limit <= 0:
        raise PlatformAdminError("public_plan_bot_limit_invalid")
    if normalized_is_featured and not normalized_is_public:
        raise PlatformAdminError("featured_plan_must_be_public")

    normalized_plan_id = int(plan_id or 0)
    plan = db.get(SaasPlan, normalized_plan_id) if normalized_plan_id > 0 else None
    if normalized_plan_id > 0 and plan is None:
        raise PlatformAdminError("plan_not_found")
    duplicate_query = select(SaasPlan).where(SaasPlan.code == normalized_code)
    if plan is not None:
        duplicate_query = duplicate_query.where(SaasPlan.id != plan.id)
    duplicate = db.scalar(duplicate_query)
    if duplicate is not None:
        raise PlatformAdminError("plan_code_exists")
    if plan is None:
        # 唯一性检查必须发生在把新对象加入 Session 之前，避免 autoflush 后把对象自身误判为重复编码。
        plan = SaasPlan(code=normalized_code, name=normalized_name)
        db.add(plan)

    plan.code = normalized_code
    plan.name = normalized_name
    plan.provider_node_limit = normalized_provider_node_limit
    plan.bot_limit = normalized_bot_limit
    plan.monthly_price_cents = normalized_monthly_price
    plan.yearly_price_cents = normalized_yearly_price
    plan.trial_days = normalized_trial_days
    plan.is_public = normalized_is_public
    plan.is_featured = normalized_is_featured
    plan.display_order = normalized_display_order
    plan.status = normalized_status
    plan.description = (description or "").strip() or None
    db.flush()
    _audit(
        db,
        actor_user_id=actor_user_id,
        action="plan.save",
        target_type="saas_plan",
        target_id=plan.id,
        detail=(
            f"code={plan.code};status={plan.status};bot_limit={plan.bot_limit};"
            f"monthly_price_cents={plan.monthly_price_cents};yearly_price_cents={plan.yearly_price_cents};"
            f"provider_node_limit={plan.provider_node_limit};is_public={str(plan.is_public).lower()}"
        ),
    )
    db.commit()
    db.refresh(plan)
    return plan


def create_tenant_with_subscription(
    db: Session,
    *,
    name: str,
    plan_id: int,
    expires_at: datetime | None,
    admin_mobile: str,
    actor_user_id: int | None = None,
) -> CreatedTenant:
    """用最少平台输入创建租户、当前订阅和首个管理员。"""

    normalized_name = (name or "").strip()
    try:
        normalized_admin_mobile = normalize_mobile(admin_mobile)
    except RegistrationError as exc:
        raise PlatformAdminError(str(exc)) from exc
    if not normalized_name:
        raise PlatformAdminError("tenant_name_required")
    if db.scalar(select(TenantUser.id).where(TenantUser.mobile == normalized_admin_mobile)) is not None:
        raise PlatformAdminError("mobile_already_registered")

    plan = db.get(SaasPlan, int(plan_id or 0))
    if plan is None:
        raise PlatformAdminError("plan_not_found")
    if plan.status != "active":
        raise PlatformAdminError("plan_disabled")

    current_time = now_shanghai()
    normalized_expiry = ensure_shanghai_datetime(expires_at) if expires_at is not None else None
    if normalized_expiry is not None and normalized_expiry <= current_time:
        raise PlatformAdminError("subscription_expiry_invalid")

    initial_password = generate_initial_password()
    try:
        # 平台员工不再手工维护租户编码。高熵随机编码不会泄露企业名称，
        # 也避免因为拼音、简称或历史命名规则产生冲突。
        generated_code = generate_tenant_code(db)
        tenant = Tenant(
            code=generated_code,
            name=normalized_name,
            status="active",
            contact_name="企业管理员",
            contact_phone=normalized_admin_mobile,
            notes=None,
        )
        db.add(tenant)
        db.flush()

        subscription = TenantSubscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            status="active",
            starts_at=current_time,
            expires_at=normalized_expiry,
        )
        admin = TenantUser(
            tenant_id=tenant.id,
            username="owner",
            mobile=normalized_admin_mobile,
            display_name="企业管理员",
            role="tenant_admin",
            password_hash=hash_password(initial_password),
            is_enabled=True,
            auth_version=1,
            must_change_password=True,
            is_owner=True,
        )
        db.add_all([subscription, admin])
        db.flush()
        _audit(
            db,
            actor_user_id=actor_user_id,
            action="tenant.create",
            target_type="tenant",
            target_id=tenant.id,
            tenant_id=tenant.id,
            # 审计只说明已经签发初始凭证，绝不保存凭证原文或可逆内容。
            detail=f"plan_id={plan.id};initial_admin_id={admin.id};initial_credential_created=true",
        )
        db.commit()
        return CreatedTenant(
            tenant_id=int(tenant.id),
            admin_user_id=int(admin.id),
            initial_password=initial_password,
        )
    except IntegrityError as exc:
        # 手机号和随机租户编码仍有数据库唯一约束，并发冲突必须转换为稳定业务错误。
        db.rollback()
        if db.scalar(select(TenantUser.id).where(TenantUser.mobile == normalized_admin_mobile)) is not None:
            raise PlatformAdminError("mobile_already_registered") from exc
        raise PlatformAdminError("tenant_create_conflict") from exc
    except BaseException:
        db.rollback()
        raise


def get_tenant_detail(db: Session, tenant_id: int) -> Tenant | None:
    """读取租户及订阅、套餐和员工，用于平台详情页。"""

    return db.scalar(
        select(Tenant)
        .where(Tenant.id == int(tenant_id))
        .options(
            selectinload(Tenant.subscriptions).selectinload(TenantSubscription.plan),
            selectinload(Tenant.users),
        )
    )


def current_subscription(tenant: Tenant) -> TenantSubscription | None:
    """按开始时间和主键选出最新订阅，保留历史记录但只展示一个当前值。"""

    subscriptions = list(tenant.subscriptions or ())
    if not subscriptions:
        return None
    return max(subscriptions, key=lambda item: (ensure_shanghai_datetime(item.starts_at), int(item.id or 0)))


def list_tenants(db: Session) -> list[Tenant]:
    """返回租户列表并预加载订阅，避免模板逐行触发额外查询。"""

    return db.scalars(
        select(Tenant)
        .options(selectinload(Tenant.subscriptions).selectinload(TenantSubscription.plan))
        .order_by(Tenant.id.desc())
    ).all()


def set_tenant_status(
    db: Session,
    *,
    tenant_id: int,
    status: str,
    actor_user_id: int | None = None,
) -> Tenant:
    """启用或停用租户；停用后现有登录会话由认证加载阶段立即拒绝。"""

    normalized_status = (status or "").strip().lower()
    if normalized_status not in {"active", "disabled"}:
        raise PlatformAdminError("tenant_status_invalid")
    tenant = db.get(Tenant, int(tenant_id))
    if tenant is None:
        raise PlatformAdminError("tenant_not_found")
    tenant.status = normalized_status
    _audit(
        db,
        actor_user_id=actor_user_id,
        action="tenant.status",
        target_type="tenant",
        target_id=tenant.id,
        tenant_id=tenant.id,
        detail=f"status={normalized_status}",
    )
    db.commit()
    db.refresh(tenant)
    return tenant


def renew_tenant_subscription(
    db: Session,
    *,
    tenant_id: int,
    plan_id: int,
    expires_at: datetime | None,
    actor_user_id: int | None = None,
) -> TenantSubscription:
    """追加一条新的当前订阅，历史订阅同步标记为过期。"""

    tenant = get_tenant_detail(db, tenant_id)
    if tenant is None:
        raise PlatformAdminError("tenant_not_found")
    plan = db.get(SaasPlan, int(plan_id))
    if plan is None:
        raise PlatformAdminError("plan_not_found")
    if plan.status != "active":
        raise PlatformAdminError("plan_disabled")

    current_time = now_shanghai()
    normalized_expiry = ensure_shanghai_datetime(expires_at) if expires_at is not None else None
    if normalized_expiry is not None and normalized_expiry <= current_time:
        raise PlatformAdminError("subscription_expiry_invalid")
    for item in tenant.subscriptions:
        if item.status == "active":
            item.status = "expired"
    subscription = TenantSubscription(
        tenant_id=tenant.id,
        plan_id=plan.id,
        status="active",
        starts_at=current_time,
        expires_at=normalized_expiry,
    )
    db.add(subscription)
    db.flush()
    _audit(
        db,
        actor_user_id=actor_user_id,
        action="tenant.renew",
        target_type="tenant_subscription",
        target_id=subscription.id,
        tenant_id=tenant.id,
        detail=f"plan_id={plan.id};expires_at={normalized_expiry.isoformat() if normalized_expiry else 'none'}",
    )
    db.commit()
    db.refresh(subscription)
    return subscription


def reset_tenant_admin_password(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    actor_user_id: int | None = None,
) -> str:
    """重置指定租户管理员密码并吊销其全部旧会话，原文只返回一次。"""

    user = db.scalar(
        select(TenantUser).where(
            TenantUser.id == int(user_id),
            TenantUser.tenant_id == int(tenant_id),
            TenantUser.role == "tenant_admin",
        )
    )
    if user is None:
        raise PlatformAdminError("tenant_admin_not_found")
    initial_password = generate_initial_password()
    # 密码、认证版本、会话吊销和平台审计必须在同一次提交中落库，避免服务异常时
    # 出现“旧会话已吊销但新密码尚未保存”的半完成状态。
    db.execute(
        update(TenantSession)
        .where(TenantSession.tenant_user_id == user.id, TenantSession.revoked_at.is_(None))
        .values(revoked_at=now_shanghai(), revoke_reason="platform_password_reset")
    )
    user.password_hash = hash_password(initial_password)
    user.auth_version += 1
    user.must_change_password = True
    _audit(
        db,
        actor_user_id=actor_user_id,
        action="tenant.admin_password_reset",
        target_type="tenant_user",
        target_id=user.id,
        tenant_id=user.tenant_id,
        detail="initial_credential_created=true;all_sessions_revoked=true",
    )
    db.commit()
    return initial_password


def dashboard_stats(db: Session) -> dict[str, int]:
    """提供平台首页需要的低成本核心统计。"""

    return {
        "tenant_total": int(db.scalar(select(func.count(Tenant.id))) or 0),
        "tenant_active": int(db.scalar(select(func.count(Tenant.id)).where(Tenant.status == "active")) or 0),
        "plan_total": int(db.scalar(select(func.count(SaasPlan.id))) or 0),
        "user_total": int(db.scalar(select(func.count(TenantUser.id))) or 0),
    }
