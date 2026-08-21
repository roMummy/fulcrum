from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.time import now_shanghai
from app.models.saas import PlatformUser, Tenant, TenantUser
from app.services.csrf import verify_csrf_token
from app.services.saas_auth import (
    authenticate_platform_user,
    authenticate_tenant_account,
    create_platform_session,
    create_tenant_session,
    generate_initial_password,
    has_tenant_role,
    hash_password,
    load_platform_principal,
    load_tenant_principal,
    revoke_platform_sessions,
    revoke_platform_session_by_token,
    revoke_tenant_sessions,
    revoke_tenant_session_by_token,
    verify_password,
)


@pytest.fixture()
def db(tmp_path):
    """认证测试使用独立数据库，确保令牌和吊销状态不会跨用例泄漏。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'saas-auth.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _create_tenant_user(db: Session, *, role: str = "operator") -> tuple[Tenant, TenantUser]:
    """创建处于可登录状态的租户和员工。"""

    tenant = Tenant(code="alpha", name="甲租户", status="active")
    db.add(tenant)
    db.flush()
    user = TenantUser(
        tenant_id=tenant.id,
        username="ops",
        mobile="13800138001",
        display_name="运营人员",
        role=role,
        password_hash=hash_password("SaaS-secret-123"),
        is_enabled=True,
        auth_version=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return tenant, user


def test_argon2_password_and_initial_password_are_one_way() -> None:
    """SaaS 密码必须使用专用慢哈希，初始密码只以原文返回一次。"""

    password_hash = hash_password("SaaS-secret-123")
    initial_password = generate_initial_password()

    assert password_hash.startswith("$argon2id$")
    assert verify_password("SaaS-secret-123", password_hash)
    assert not verify_password("wrong-password", password_hash)
    assert len(initial_password) >= 20
    assert verify_password(initial_password, hash_password(initial_password))


def test_tenant_session_is_revoked_after_role_change(db: Session) -> None:
    """角色变化后 auth_version 不一致，旧会话必须立即失效。"""

    _tenant, user = _create_tenant_user(db, role="operator")
    created = create_tenant_session(db, user)
    principal = load_tenant_principal(db, created.raw_token)

    assert principal is not None
    assert principal.role == "operator"
    assert has_tenant_role(principal, "readonly")
    assert not has_tenant_role(principal, "tenant_admin")
    assert not has_tenant_role(principal, "unknown-role")

    user.role = "readonly"
    user.auth_version += 1
    db.commit()

    assert load_tenant_principal(db, created.raw_token) is None


def test_tenant_session_can_be_explicitly_revoked(db: Session) -> None:
    """禁用账号或修改密码时可以批量吊销该员工全部数据库会话。"""

    _tenant, user = _create_tenant_user(db)
    first = create_tenant_session(db, user)
    second = create_tenant_session(db, user)

    assert revoke_tenant_sessions(db, user.id, reason="password_changed") == 2
    assert load_tenant_principal(db, first.raw_token) is None
    assert load_tenant_principal(db, second.raw_token) is None


def test_platform_session_uses_an_independent_identity_chain(db: Session) -> None:
    """平台账号会话不能通过租户认证入口恢复，认证版本变化同样即时失效。"""

    user = PlatformUser(
        username="platform",
        display_name="平台管理员",
        password_hash=hash_password("Platform-secret-123"),
        role="platform_admin",
        is_enabled=True,
        auth_version=1,
    )
    db.add(user)
    db.commit()
    created = create_platform_session(db, user)

    principal = load_platform_principal(db, created.raw_token)
    assert principal is not None
    assert principal.role == "platform_admin"
    assert load_tenant_principal(db, created.raw_token) is None

    assert revoke_platform_sessions(db, user.id, reason="manual_logout") == 1
    assert load_platform_principal(db, created.raw_token) is None


def test_csrf_requires_cookie_form_and_database_hash_to_match(db: Session) -> None:
    """CSRF 必须同时匹配同站 Cookie、表单值和当前数据库会话保存的摘要。"""

    _tenant, user = _create_tenant_user(db)
    created = create_tenant_session(db, user, expires_at=now_shanghai() + timedelta(hours=2))

    assert verify_csrf_token(db, created.raw_token, created.raw_csrf_token, created.raw_csrf_token)
    assert not verify_csrf_token(db, created.raw_token, created.raw_csrf_token, "伪造值")
    assert not verify_csrf_token(db, "伪造会话", created.raw_csrf_token, created.raw_csrf_token)


def test_platform_and_tenant_credentials_are_queried_in_separate_identity_tables(db: Session) -> None:
    """平台用户名和企业手机号只能查询各自身份表，不能跨身份链认证。"""

    _tenant, tenant_user = _create_tenant_user(db)
    platform_user = PlatformUser(
        username=tenant_user.username,
        display_name="平台同名管理员",
        password_hash=hash_password("Platform-secret-123"),
        role="platform_admin",
        is_enabled=True,
        auth_version=1,
    )
    db.add(platform_user)
    db.commit()

    assert authenticate_tenant_account(db, "13800138001", "SaaS-secret-123") == tenant_user
    assert authenticate_tenant_account(db, "13900139000", "SaaS-secret-123") is None
    assert authenticate_platform_user(db, "ops", "Platform-secret-123") == platform_user
    assert authenticate_platform_user(db, "ops", "SaaS-secret-123") is None


def test_logout_revokes_only_the_current_raw_session_token(db: Session) -> None:
    """单设备注销不能误伤同一账号仍在使用的其他数据库会话。"""

    _tenant, tenant_user = _create_tenant_user(db)
    tenant_first = create_tenant_session(db, tenant_user)
    tenant_second = create_tenant_session(db, tenant_user)
    assert revoke_tenant_session_by_token(db, tenant_first.raw_token)
    assert load_tenant_principal(db, tenant_first.raw_token) is None
    assert load_tenant_principal(db, tenant_second.raw_token) is not None

    platform_user = PlatformUser(
        username="platform-current",
        display_name="平台管理员",
        password_hash=hash_password("Platform-secret-123"),
        role="platform_admin",
        is_enabled=True,
        auth_version=1,
    )
    db.add(platform_user)
    db.commit()
    platform_first = create_platform_session(db, platform_user)
    platform_second = create_platform_session(db, platform_user)
    assert revoke_platform_session_by_token(db, platform_first.raw_token)
    assert load_platform_principal(db, platform_first.raw_token) is None
    assert load_platform_principal(db, platform_second.raw_token) is not None
