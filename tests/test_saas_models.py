from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.time import now_shanghai
from app.models.saas import (
    LOGIN_PROXY_MODES,
    LOGIN_PROXY_SESSION_STATUSES,
    PLATFORM_ROLES,
    PLAN_STATUSES,
    PROXY_HEALTH_STATUSES,
    SUBSCRIPTION_STATUSES,
    TENANT_ROLES,
    TENANT_STATUSES,
    LoginProxySession,
    PlatformAuditLog,
    PlatformProxy,
    PlatformSession,
    PlatformUser,
    SaasPlan,
    Tenant,
    TenantSession,
    TenantSubscription,
    TenantUser,
)


@pytest.fixture()
def db(tmp_path):
    """为每个模型用例建立独立数据库，避免唯一约束测试相互污染。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'saas-models.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _create_tenant(db: Session, *, code: str, name: str) -> Tenant:
    """创建最小租户并立即取得主键，供员工和订阅约束测试复用。"""

    tenant = Tenant(code=code, name=name, status="active")
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def test_saas_identity_tables_are_registered_in_shared_metadata() -> None:
    """平台身份与租户身份必须进入统一元数据，才能被 Alembic 完整管理。"""

    expected_tables = {
        "platform_users",
        "platform_sessions",
        "tenants",
        "saas_plans",
        "tenant_subscriptions",
        "tenant_users",
        "tenant_sessions",
        "platform_audit_logs",
    }

    assert expected_tables.issubset(Base.metadata.tables)
    assert PLATFORM_ROLES == frozenset({"platform_admin"})
    assert TENANT_ROLES == frozenset({"tenant_admin", "operator", "readonly"})
    assert TENANT_STATUSES == frozenset({"active", "disabled"})
    assert PLAN_STATUSES == frozenset({"active", "disabled"})
    assert SUBSCRIPTION_STATUSES == frozenset({"active", "expired", "suspended"})


def test_login_proxy_pool_tables_and_status_sets_are_registered() -> None:
    """平台代理和租户登录会话必须进入统一元数据，并使用唯一状态集合。"""

    assert {
        "platform_proxies",
        "login_proxy_sessions",
        "proxy_connectors",
        "proxy_connector_activation_codes",
        "proxy_connector_credentials",
    }.issubset(Base.metadata.tables)
    assert PROXY_HEALTH_STATUSES == frozenset({"healthy", "unhealthy"})
    assert LOGIN_PROXY_MODES == frozenset({"direct", "platform_pool", "custom", "local_connector"})
    assert LOGIN_PROXY_SESSION_STATUSES == frozenset(
        {"preparing", "qrcode_ready", "completed", "failed", "cancelled", "expired"}
    )


def test_platform_proxy_endpoint_is_unique(db: Session) -> None:
    """同一 SOCKS 端点和用户名只能进入代理池一次，避免批量导入重复分配。"""

    db.add_all(
        [
            PlatformProxy(
                name="杭州代理一",
                province="浙江省",
                city="杭州市",
                host="127.0.0.1",
                port=1080,
                username="proxy-user",
            ),
            PlatformProxy(
                name="杭州代理二",
                province="浙江省",
                city="杭州市",
                host="127.0.0.1",
                port=1080,
                username="proxy-user",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_platform_proxy_port_must_be_valid(db: Session) -> None:
    """代理端口必须由数据库约束限制在网络端口范围内。"""

    db.add(
        PlatformProxy(
            name="非法端口代理",
            province="浙江省",
            city="杭州市",
            host="127.0.0.1",
            port=70000,
            username="",
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_login_proxy_session_keeps_channel_and_tenant_ownership(db: Session) -> None:
    """登录代理会话必须同时关联可信租户和渠道，不能成为平台范围的游离记录。"""

    # SQLite 测试连接默认不强制外键，因此直接检查 ORM 和真实表结构；MariaDB
    # 部署迁移还会通过外键阻止不存在的租户或渠道主键。
    columns = {column["name"]: column for column in inspect(db.bind).get_columns("login_proxy_sessions")}
    assert columns["tenant_id"]["nullable"] is False
    assert columns["channel_id"]["nullable"] is False
    assert LoginProxySession.__table__.c.tenant_id.foreign_keys
    assert LoginProxySession.__table__.c.channel_id.foreign_keys


def test_same_username_is_allowed_in_different_tenants(db: Session) -> None:
    """不同租户可以使用相同员工登录名，避免平台范围的无意义占用。"""

    first = _create_tenant(db, code="alpha", name="甲租户")
    second = _create_tenant(db, code="beta", name="乙租户")
    db.add_all(
        [
            TenantUser(
                tenant_id=first.id,
                username="ops",
                display_name="甲运营",
                role="operator",
                password_hash="hash-a",
            ),
            TenantUser(
                tenant_id=second.id,
                username="ops",
                display_name="乙运营",
                role="operator",
                password_hash="hash-b",
            ),
        ]
    )

    db.commit()


def test_same_username_is_rejected_inside_one_tenant(db: Session) -> None:
    """同一租户内的员工登录名必须唯一，数据库约束负责并发兜底。"""

    tenant = _create_tenant(db, code="alpha", name="甲租户")
    db.add_all(
        [
            TenantUser(
                tenant_id=tenant.id,
                username="ops",
                display_name="运营一",
                role="operator",
                password_hash="hash-a",
            ),
            TenantUser(
                tenant_id=tenant.id,
                username="ops",
                display_name="运营二",
                role="readonly",
                password_hash="hash-b",
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_subscription_and_session_models_keep_security_boundaries(db: Session) -> None:
    """订阅有效期和数据库会话的失效字段必须由服务端持久化，不能依赖 Cookie 自报。"""

    tenant = _create_tenant(db, code="alpha", name="甲租户")
    plan = SaasPlan(code="starter", name="入门版", provider_node_limit=2, status="active")
    user = TenantUser(
        tenant_id=tenant.id,
        username="admin",
        display_name="租户管理员",
        role="tenant_admin",
        password_hash="hash-user",
    )
    platform_user = PlatformUser(
        username="platform",
        display_name="平台管理员",
        role="platform_admin",
        password_hash="hash-platform",
    )
    db.add_all([plan, user, platform_user])
    db.commit()

    expires_at = now_shanghai() + timedelta(days=30)
    db.add_all(
        [
            TenantSubscription(
                tenant_id=tenant.id,
                plan_id=plan.id,
                status="active",
                starts_at=now_shanghai(),
                expires_at=expires_at,
            ),
            TenantSession(
                tenant_id=tenant.id,
                tenant_user_id=user.id,
                session_token_hash="tenant-token-hash",
                csrf_token_hash="tenant-csrf-hash",
                auth_version=user.auth_version,
                expires_at=expires_at,
            ),
            PlatformSession(
                platform_user_id=platform_user.id,
                session_token_hash="platform-token-hash",
                csrf_token_hash="platform-csrf-hash",
                auth_version=platform_user.auth_version,
                expires_at=expires_at,
            ),
            PlatformAuditLog(
                actor_user_id=platform_user.id,
                action="tenant.create",
                target_type="tenant",
                target_id=tenant.id,
                target_tenant_id=tenant.id,
                detail="创建租户",
            ),
        ]
    )
    db.commit()

    tenant_session_columns = {column["name"] for column in inspect(db.bind).get_columns("tenant_sessions")}
    assert {
        "session_token_hash",
        "csrf_token_hash",
        "auth_version",
        "expires_at",
        "last_seen_at",
        "revoked_at",
        "revoke_reason",
    }.issubset(tenant_session_columns)
    LoginProxySession,
    PlatformProxy,
