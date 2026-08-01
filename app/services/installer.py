from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.saas import PlatformUser
from app.services.database_lifecycle import assert_database_at_head
from app.services.saas_auth import hash_password


def is_setup_required(db: Session) -> bool:
    """平台身份表没有可用管理员时才进入首次管理员创建页。"""

    admin_count = db.scalar(
        select(PlatformUser)
        .where(PlatformUser.role == "platform_admin", PlatformUser.is_enabled.is_(True))
        .limit(1)
    )
    return admin_count is None


def provision_platform_admin(
    db: Session,
    *,
    admin_username: str,
    admin_password: str,
    admin_display_name: str,
) -> str | None:
    """只在已迁移数据库中创建首个平台管理员，绝不建表或修改连接配置。"""

    try:
        assert_database_at_head(db.connection())
        normalized_username = str(admin_username or "").strip()
        if not normalized_username:
            return "platform_admin_username_required"
        if db.scalar(select(PlatformUser.id).limit(1)) is not None:
            return "platform_admin_already_exists"
        db.add(
            PlatformUser(
                username=normalized_username,
                display_name=str(admin_display_name or "").strip() or "平台管理员",
                password_hash=hash_password(admin_password),
                role="platform_admin",
                is_enabled=True,
                auth_version=1,
                must_change_password=True,
            )
        )
        db.commit()
        return None
    except Exception as exc:
        db.rollback()
        return str(exc)


def ensure_bootstrap_platform_admin(db: Session) -> None:
    """按受控环境变量创建首个平台管理员；密码只参与哈希，不写日志。"""

    if not is_setup_required(db) or not settings.has_bootstrap_admin_config:
        return
    error = provision_platform_admin(
        db,
        admin_username=settings.auth_bootstrap_admin_username,
        admin_password=settings.auth_bootstrap_admin_password,
        admin_display_name=settings.auth_bootstrap_admin_display_name,
    )
    if error:
        raise RuntimeError(error)
