"""Fulcrum 企业自助注册和试用开通事务。"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.saas import PlatformAuditLog, SaasPlan, Tenant, TenantSubscription, TenantUser
from app.services.saas_auth import hash_password


MOBILE_PATTERN = re.compile(r"^1[3-9]\d{9}$")
MOBILE_SEPARATOR_PATTERN = re.compile(r"[\s()\-]")


class RegistrationError(ValueError):
    """公开注册输入或平台试用配置不满足业务规则。"""


@dataclass(frozen=True)
class RegisteredTrialTenant:
    """注册成功后的可信主键回执，不包含密码、手机号或会话原始令牌。"""

    tenant_id: int
    owner_user_id: int
    subscription_id: int


def normalize_mobile(value: str) -> str:
    """规范化中国大陆手机号，并拒绝通过删字符掩盖的异常输入。"""

    normalized = MOBILE_SEPARATOR_PATTERN.sub("", (value or "").strip())
    if normalized.startswith("+86"):
        normalized = normalized[3:]
    elif normalized.startswith("0086"):
        normalized = normalized[4:]
    if not MOBILE_PATTERN.fullmatch(normalized):
        raise RegistrationError("mobile_invalid")
    return normalized


def generate_tenant_code(db: Session) -> str:
    """生成不包含企业名称或联系人信息的高熵企业编码。"""

    # 随机编码避免企业名称拼音碰撞，也不在登录 URL 中泄露客户名称。数据库查询用于处理极低概率碰撞。
    for _ in range(16):
        candidate = f"org-{secrets.token_hex(8)}"
        if db.scalar(select(Tenant.id).where(Tenant.code == candidate)) is None:
            return candidate
    raise RegistrationError("tenant_code_generation_failed")


def _validate_password(password: str) -> str:
    """收敛首版密码长度，避免空密码和超长输入消耗异常哈希资源。"""

    normalized = password or ""
    if len(normalized) < 8:
        raise RegistrationError("password_too_short")
    if len(normalized) > 128:
        raise RegistrationError("password_too_long")
    return normalized


def register_trial_tenant(
    db: Session,
    *,
    mobile: str,
    password: str,
    enterprise_name: str,
    contact_name: str,
) -> RegisteredTrialTenant:
    """在一次提交中创建企业、所有者、七天试用订阅和平台审计。"""

    normalized_mobile = normalize_mobile(mobile)
    normalized_password = _validate_password(password)
    normalized_enterprise_name = (enterprise_name or "").strip()
    normalized_contact_name = (contact_name or "").strip()
    if not normalized_enterprise_name or len(normalized_enterprise_name) > 150:
        raise RegistrationError("enterprise_name_invalid")
    if not normalized_contact_name or len(normalized_contact_name) > 100:
        raise RegistrationError("contact_name_invalid")
    if db.scalar(select(TenantUser.id).where(TenantUser.mobile == normalized_mobile)) is not None:
        raise RegistrationError("mobile_already_registered")

    trial_plan = db.scalar(select(SaasPlan).where(SaasPlan.code == "trial"))
    if trial_plan is None:
        raise RegistrationError("trial_plan_not_found")
    if trial_plan.status != "active":
        raise RegistrationError("trial_plan_disabled")
    if trial_plan.trial_days <= 0 or trial_plan.bot_limit <= 0:
        raise RegistrationError("trial_plan_invalid")

    current_time = now_shanghai()
    try:
        tenant = Tenant(
            code=generate_tenant_code(db),
            name=normalized_enterprise_name,
            status="active",
            contact_name=normalized_contact_name,
            contact_phone=normalized_mobile,
        )
        db.add(tenant)
        db.flush()

        # 故意在租户 flush 后执行密码哈希，用测试覆盖任何中途失败都能回滚已分配的主键和根记录。
        owner = TenantUser(
            tenant_id=tenant.id,
            username="owner",
            mobile=normalized_mobile,
            display_name=normalized_contact_name,
            password_hash=hash_password(normalized_password),
            role="tenant_admin",
            is_enabled=True,
            auth_version=1,
            must_change_password=False,
            is_owner=True,
        )
        subscription = TenantSubscription(
            tenant_id=tenant.id,
            plan_id=trial_plan.id,
            status="active",
            starts_at=current_time,
            expires_at=current_time + timedelta(days=trial_plan.trial_days),
            notes="self_service_trial",
        )
        db.add_all([owner, subscription])
        db.flush()
        db.add(
            PlatformAuditLog(
                actor_user_id=None,
                action="tenant.self_register",
                target_type="tenant",
                target_id=tenant.id,
                target_tenant_id=tenant.id,
                # 审计只记录试用配置，不写手机号、密码或其他可用于登录的内容。
                detail=f"plan_id={trial_plan.id};trial_days={trial_plan.trial_days};source=self_service",
            )
        )
        db.commit()
        return RegisteredTrialTenant(
            tenant_id=int(tenant.id),
            owner_user_id=int(owner.id),
            subscription_id=int(subscription.id),
        )
    except IntegrityError as exc:
        # 数据库唯一约束是并发注册的最终防线；回滚后再映射为稳定的公开错误码。
        db.rollback()
        if db.scalar(select(TenantUser.id).where(TenantUser.mobile == normalized_mobile)) is not None:
            raise RegistrationError("mobile_already_registered") from exc
        raise RegistrationError("registration_conflict") from exc
    except BaseException:
        db.rollback()
        raise
