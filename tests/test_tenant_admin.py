from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.saas import Tenant, TenantUser
from app.services.saas_auth import TenantPrincipal, create_tenant_session, hash_password, load_tenant_principal
from app.services.tenant_admin import (
    TenantAdminError,
    TenantPolicyError,
    create_tenant_user,
    list_tenant_users,
    require_tenant_mutation_allowed,
    reset_tenant_user_password,
    update_tenant_user,
)
from app.web.tenant_admin import _require_tenant_admin


@pytest.fixture()
def tenant_factory(tmp_path):
    """员工管理测试同时需要平台建租户和租户作用域内维护员工。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'tenant-admin.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        db.add_all([alpha, beta])
        db.flush()
        alpha_admin = TenantUser(
            tenant_id=alpha.id,
            username="admin",
            mobile="13400134001",
            display_name="甲管理员",
            role="tenant_admin",
            password_hash=hash_password("Tenant-admin-123"),
            is_enabled=True,
            must_change_password=False,
        )
        beta_admin = TenantUser(
            tenant_id=beta.id,
            username="admin",
            mobile="13400134002",
            display_name="乙管理员",
            role="tenant_admin",
            password_hash=hash_password("Tenant-admin-123"),
            is_enabled=True,
            must_change_password=False,
        )
        db.add_all([alpha_admin, beta_admin])
        db.commit()
        ids = {
            "alpha": int(alpha.id),
            "beta": int(beta.id),
            "alpha_admin": int(alpha_admin.id),
            "beta_admin": int(beta_admin.id),
        }
    try:
        yield factory, ids
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _principal(**overrides) -> TenantPrincipal:
    """构造只用于订阅策略判断的可信身份快照。"""

    values = {
        "session_id": 1,
        "tenant_id": 1,
        "user_id": 1,
        "username": "admin",
        "display_name": "管理员",
        "role": "tenant_admin",
        "must_change_password": False,
        "tenant_status": "active",
        "subscription_status": "active",
        "subscription_expires_at": now_shanghai() + timedelta(days=30),
    }
    values.update(overrides)
    return TenantPrincipal(**values)


def test_tenant_admin_creates_employee_and_auth_changes_revoke_old_sessions(tenant_factory) -> None:
    """员工角色或手机号变化后旧会话必须立即失效，初始密码只以哈希形式持久化。"""

    factory, ids = tenant_factory
    with tenant_scope(ids["alpha"]), tenant_session(factory) as db:
        created = create_tenant_user(
            db,
            username="ops",
            mobile="13400134003",
            display_name="运营人员",
            role="operator",
        )
        user = db.get(TenantUser, created.user_id)
        session = create_tenant_session(db, user)
        assert load_tenant_principal(db, session.raw_token) is not None

        update_tenant_user(
            db,
            user_id=user.id,
            mobile="13400134003",
            display_name="只读人员",
            role="readonly",
            is_enabled=True,
        )
        assert load_tenant_principal(db, session.raw_token) is None

        # 角色更新后重新登录，再仅修改手机号，验证手机号同样属于即时生效的认证属性。
        db.refresh(user)
        mobile_session = create_tenant_session(db, user)
        update_tenant_user(
            db,
            user_id=user.id,
            mobile="13400134004",
            display_name="只读人员",
            role="readonly",
            is_enabled=True,
        )
        assert load_tenant_principal(db, mobile_session.raw_token) is None
        assert db.get(TenantUser, user.id).mobile == "13400134004"


def test_tenant_admin_cannot_remove_last_admin_or_operate_other_tenant_user(tenant_factory) -> None:
    """最后一个启用管理员和其他租户员工主键都必须受到保护。"""

    factory, ids = tenant_factory
    with tenant_scope(ids["alpha"]), tenant_session(factory) as db:
        with pytest.raises(TenantAdminError, match="last_tenant_admin_required"):
            update_tenant_user(
                db,
                user_id=ids["alpha_admin"],
                mobile="13400134001",
                display_name="甲管理员",
                role="readonly",
                is_enabled=True,
            )
        with pytest.raises(TenantAdminError, match="tenant_user_not_found"):
            reset_tenant_user_password(db, user_id=ids["beta_admin"])
        assert [user.username for user in list_tenant_users(db)] == ["admin"]


def test_tenant_employee_mobile_is_required_and_globally_unique(tenant_factory) -> None:
    """企业员工手机号用于直接恢复租户身份，因此不允许缺失或跨租户复用。"""

    factory, ids = tenant_factory
    with tenant_scope(ids["alpha"]), tenant_session(factory) as db:
        with pytest.raises(TenantAdminError, match="mobile_invalid"):
            create_tenant_user(db, username="missing", mobile="", display_name="缺手机号", role="readonly")
        with pytest.raises(TenantAdminError, match="mobile_already_registered"):
            create_tenant_user(
                db,
                username="duplicate",
                mobile="13400134002",
                display_name="重复手机号",
                role="readonly",
            )


def test_subscription_policy_keeps_reads_but_rejects_disabled_suspended_and_expired_mutations() -> None:
    """统一策略只判断写权限，页面 GET 是否放行由中间件的安全方法分支控制。"""

    require_tenant_mutation_allowed(_principal())
    with pytest.raises(TenantPolicyError, match="tenant_disabled"):
        require_tenant_mutation_allowed(_principal(tenant_status="disabled"))
    with pytest.raises(TenantPolicyError, match="tenant_subscription_suspended"):
        require_tenant_mutation_allowed(_principal(subscription_status="suspended"))
    with pytest.raises(TenantPolicyError, match="tenant_expired"):
        require_tenant_mutation_allowed(
            _principal(subscription_expires_at=now_shanghai() - timedelta(seconds=1))
        )


def test_operator_cannot_enter_tenant_admin_mutation_route() -> None:
    """Web 层不能仅依赖页面隐藏按钮，运营角色直接调用路由仍应返回 403。"""

    request = SimpleNamespace(state=SimpleNamespace(current_user=_principal(role="operator")))
    with pytest.raises(HTTPException) as exc_info:
        _require_tenant_admin(request)
    assert exc_info.value.status_code == 403
