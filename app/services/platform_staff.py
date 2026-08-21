"""SaaS 平台员工账号管理服务。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.saas import PlatformAuditLog, PlatformSession, PlatformUser
from app.services.saas_auth import generate_initial_password, hash_password


class PlatformStaffError(ValueError):
    """平台员工输入或账号治理规则不满足。"""


@dataclass(frozen=True)
class CreatedPlatformUser:
    """平台员工创建回执；初始密码只能由当前成功响应读取一次。"""

    user_id: int
    initial_password: str


def _normalize_username(username: str) -> str:
    """收敛平台用户名并限制到数据库字段长度。"""

    normalized = (username or "").strip()
    if not normalized or len(normalized) > 100:
        raise PlatformStaffError("platform_username_invalid")
    return normalized


def _normalize_display_name(display_name: str) -> str:
    """校验平台员工展示名称，避免空白或超长内容进入导航身份区。"""

    normalized = (display_name or "").strip()
    if not normalized or len(normalized) > 100:
        raise PlatformStaffError("platform_display_name_invalid")
    return normalized


def _audit_staff_change(
    db: Session,
    *,
    actor_user_id: int | None,
    action: str,
    target_user_id: int,
    detail: str,
) -> None:
    """记录平台账号治理动作，审计内容不包含密码或会话原文。"""

    db.add(
        PlatformAuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target_type="platform_user",
            target_id=target_user_id,
            target_tenant_id=None,
            detail=detail,
        )
    )


def _load_platform_user(db: Session, user_id: int) -> PlatformUser:
    """读取平台员工，不接受租户用户主键或其他身份类型。"""

    user = db.get(PlatformUser, int(user_id))
    if user is None:
        raise PlatformStaffError("platform_user_not_found")
    return user


def _revoke_platform_user_sessions(db: Session, user: PlatformUser, reason: str) -> None:
    """在当前事务内吊销目标平台员工全部有效会话。"""

    db.execute(
        update(PlatformSession)
        .where(
            PlatformSession.platform_user_id == user.id,
            PlatformSession.revoked_at.is_(None),
        )
        .values(revoked_at=now_shanghai(), revoke_reason=reason)
    )


def _ensure_another_enabled_admin(db: Session, user: PlatformUser) -> None:
    """禁止停用平台最后一个启用的全权限管理员。"""

    remaining = db.scalar(
        select(func.count(PlatformUser.id)).where(
            PlatformUser.role == "platform_admin",
            PlatformUser.is_enabled.is_(True),
            PlatformUser.id != user.id,
        )
    )
    if int(remaining or 0) <= 0:
        raise PlatformStaffError("last_platform_admin_required")


def list_platform_users(db: Session) -> list[PlatformUser]:
    """返回平台员工清单，启用账号优先并按主键稳定排序。"""

    return db.scalars(
        select(PlatformUser).order_by(PlatformUser.is_enabled.desc(), PlatformUser.id.asc())
    ).all()


def create_platform_user(
    db: Session,
    *,
    username: str,
    display_name: str,
    actor_user_id: int | None,
) -> CreatedPlatformUser:
    """创建固定全权限角色的平台员工，并生成一次性初始密码。"""

    normalized_username = _normalize_username(username)
    normalized_display_name = _normalize_display_name(display_name)
    if db.scalar(select(PlatformUser.id).where(PlatformUser.username == normalized_username)) is not None:
        raise PlatformStaffError("platform_username_exists")

    initial_password = generate_initial_password()
    user = PlatformUser(
        username=normalized_username,
        display_name=normalized_display_name,
        password_hash=hash_password(initial_password),
        role="platform_admin",
        is_enabled=True,
        auth_version=1,
        must_change_password=True,
    )
    db.add(user)
    try:
        db.flush()
        _audit_staff_change(
            db,
            actor_user_id=actor_user_id,
            action="platform_user.create",
            target_user_id=int(user.id),
            detail="role=platform_admin;initial_credential_created=true",
        )
        db.commit()
    except IntegrityError as exc:
        # 数据库唯一索引负责并发创建兜底，回滚后统一映射为不泄露内部结构的业务错误。
        db.rollback()
        if db.scalar(select(PlatformUser.id).where(PlatformUser.username == normalized_username)) is not None:
            raise PlatformStaffError("platform_username_exists") from exc
        raise PlatformStaffError("platform_user_conflict") from exc
    db.refresh(user)
    return CreatedPlatformUser(user_id=int(user.id), initial_password=initial_password)


def update_platform_user(
    db: Session,
    *,
    user_id: int,
    display_name: str,
    is_enabled: bool,
    actor_user_id: int | None,
) -> PlatformUser:
    """更新平台员工姓名和状态，启停变化立即使全部旧会话失效。"""

    user = _load_platform_user(db, user_id)
    normalized_display_name = _normalize_display_name(display_name)
    normalized_enabled = bool(is_enabled)
    if user.is_enabled and not normalized_enabled:
        _ensure_another_enabled_admin(db, user)

    auth_changed = user.is_enabled != normalized_enabled
    user.display_name = normalized_display_name
    user.is_enabled = normalized_enabled
    if auth_changed:
        user.auth_version += 1
        _revoke_platform_user_sessions(db, user, "platform_user_status_changed")
    _audit_staff_change(
        db,
        actor_user_id=actor_user_id,
        action="platform_user.update",
        target_user_id=int(user.id),
        detail=f"role=platform_admin;is_enabled={str(user.is_enabled).lower()}",
    )
    db.commit()
    db.refresh(user)
    return user


def reset_platform_user_password(
    db: Session,
    *,
    user_id: int,
    actor_user_id: int | None,
) -> str:
    """重置平台员工密码并吊销全部旧会话，原文只返回给当前调用方。"""

    user = _load_platform_user(db, user_id)
    initial_password = generate_initial_password()
    user.password_hash = hash_password(initial_password)
    user.auth_version += 1
    user.must_change_password = True
    _revoke_platform_user_sessions(db, user, "platform_user_password_reset")
    _audit_staff_change(
        db,
        actor_user_id=actor_user_id,
        action="platform_user.password_reset",
        target_user_id=int(user.id),
        detail="initial_credential_created=true;all_sessions_revoked=true",
    )
    db.commit()
    return initial_password
