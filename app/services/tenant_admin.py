"""租户员工管理和订阅只读策略。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import TENANT_ROLES, TenantSession, TenantUser
from app.services.saas_auth import TenantPrincipal, generate_initial_password, hash_password


class TenantAdminError(ValueError):
    """员工管理输入或租户治理规则不满足。"""


class TenantPolicyError(PermissionError):
    """租户当前状态只允许读取，不允许修改业务数据。"""


@dataclass(frozen=True)
class CreatedTenantUser:
    """新员工回执，初始密码只在本次响应中短暂存在。"""

    user_id: int
    initial_password: str


def require_tenant_mutation_allowed(principal: TenantPrincipal, now: datetime | None = None) -> None:
    """租户停用、订阅挂起或到期时拒绝写操作，但保留只读访问。"""

    current = ensure_shanghai_datetime(now) if now is not None else now_shanghai()
    if principal.tenant_status != "active":
        raise TenantPolicyError("tenant_disabled")
    if principal.subscription_status in {"expired", "suspended"}:
        raise TenantPolicyError(f"tenant_subscription_{principal.subscription_status}")
    if principal.subscription_expires_at and ensure_shanghai_datetime(principal.subscription_expires_at) <= current:
        raise TenantPolicyError("tenant_expired")


def list_tenant_users(db: Session) -> list[TenantUser]:
    """显式按当前可信 tenant_id 返回员工，SaaS 身份表不依赖业务模型混入。"""

    tenant_id = require_tenant_id()
    return db.scalars(
        select(TenantUser).where(TenantUser.tenant_id == tenant_id).order_by(TenantUser.id.asc())
    ).all()


def _load_tenant_user(db: Session, user_id: int) -> TenantUser:
    """在当前租户内解析员工，猜测其他租户主键统一表现为不存在。"""

    tenant_id = require_tenant_id()
    user = db.scalar(
        select(TenantUser).where(TenantUser.id == int(user_id), TenantUser.tenant_id == tenant_id)
    )
    if user is None:
        raise TenantAdminError("tenant_user_not_found")
    return user


def _normalize_role(role: str) -> str:
    """员工角色只允许使用首期固定集合，未知值默认拒绝。"""

    normalized = (role or "").strip().lower()
    if normalized not in TENANT_ROLES:
        raise TenantAdminError("tenant_role_invalid")
    return normalized


def _revoke_user_sessions(db: Session, user: TenantUser, reason: str) -> None:
    """在调用方事务内吊销员工全部有效会话。"""

    db.execute(
        update(TenantSession)
        .where(
            TenantSession.tenant_id == user.tenant_id,
            TenantSession.tenant_user_id == user.id,
            TenantSession.revoked_at.is_(None),
        )
        .values(revoked_at=now_shanghai(), revoke_reason=reason)
    )


def _ensure_another_enabled_admin(db: Session, user: TenantUser) -> None:
    """禁止停用或降级当前租户最后一个启用管理员。"""

    remaining = db.scalar(
        select(func.count(TenantUser.id)).where(
            TenantUser.tenant_id == user.tenant_id,
            TenantUser.role == "tenant_admin",
            TenantUser.is_enabled.is_(True),
            TenantUser.id != user.id,
        )
    )
    if int(remaining or 0) <= 0:
        raise TenantAdminError("last_tenant_admin_required")


def create_tenant_user(
    db: Session,
    *,
    username: str,
    display_name: str,
    role: str,
) -> CreatedTenantUser:
    """在当前租户创建员工，并生成只展示一次的初始密码。"""

    tenant_id = require_tenant_id()
    normalized_username = (username or "").strip()
    normalized_display_name = (display_name or "").strip()
    normalized_role = _normalize_role(role)
    if not normalized_username:
        raise TenantAdminError("username_required")
    if not normalized_display_name:
        raise TenantAdminError("display_name_required")
    if db.scalar(
        select(TenantUser.id).where(
            TenantUser.tenant_id == tenant_id,
            TenantUser.username == normalized_username,
        )
    ) is not None:
        raise TenantAdminError("username_exists")

    initial_password = generate_initial_password()
    user = TenantUser(
        tenant_id=tenant_id,
        username=normalized_username,
        display_name=normalized_display_name,
        role=normalized_role,
        password_hash=hash_password(initial_password),
        is_enabled=True,
        auth_version=1,
        must_change_password=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return CreatedTenantUser(user_id=int(user.id), initial_password=initial_password)


def update_tenant_user(
    db: Session,
    *,
    user_id: int,
    display_name: str,
    role: str,
    is_enabled: bool,
) -> TenantUser:
    """更新员工资料；权限和状态变化立即使所有旧会话失效。"""

    user = _load_tenant_user(db, user_id)
    normalized_display_name = (display_name or "").strip()
    normalized_role = _normalize_role(role)
    if not normalized_display_name:
        raise TenantAdminError("display_name_required")

    removes_admin = user.role == "tenant_admin" and user.is_enabled and (
        normalized_role != "tenant_admin" or not bool(is_enabled)
    )
    if removes_admin:
        _ensure_another_enabled_admin(db, user)

    auth_changed = user.role != normalized_role or user.is_enabled != bool(is_enabled)
    user.display_name = normalized_display_name
    user.role = normalized_role
    user.is_enabled = bool(is_enabled)
    if auth_changed:
        user.auth_version += 1
        _revoke_user_sessions(db, user, "tenant_user_permissions_changed")
    db.commit()
    db.refresh(user)
    return user


def reset_tenant_user_password(db: Session, *, user_id: int) -> str:
    """重置当前租户员工密码，并在同一事务吊销所有旧会话。"""

    user = _load_tenant_user(db, user_id)
    initial_password = generate_initial_password()
    user.password_hash = hash_password(initial_password)
    user.auth_version += 1
    user.must_change_password = True
    _revoke_user_sessions(db, user, "tenant_user_password_reset")
    db.commit()
    return initial_password
