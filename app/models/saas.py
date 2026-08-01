from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.core.time import now_shanghai
from app.models.mixins import TenantOwnedMixin, TimestampMixin


# 允许值集中定义，后续认证和平台管理服务必须在写库前复用这些集合进行校验。
PLATFORM_ROLES = frozenset({"platform_admin"})
TENANT_ROLES = frozenset({"tenant_admin", "operator", "readonly"})
TENANT_STATUSES = frozenset({"active", "disabled"})
PLAN_STATUSES = frozenset({"active", "disabled"})
SUBSCRIPTION_STATUSES = frozenset({"active", "expired", "suspended"})


class PlatformUser(Base, TimestampMixin):
    """平台运营账号，与任何租户员工身份彻底分离。"""

    __tablename__ = "platform_users"
    __table_args__ = (
        CheckConstraint("role IN ('platform_admin')", name="ck_platform_users_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="platform_admin", index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    sessions = relationship("PlatformSession", back_populates="user", cascade="all, delete-orphan")


class PlatformSession(Base, TimestampMixin):
    """平台账号数据库会话；浏览器持有原始值，服务端只保存不可逆摘要。"""

    __tablename__ = "platform_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform_user_id: Mapped[int] = mapped_column(ForeignKey("platform_users.id"), nullable=False, index=True)
    session_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 创建会话时固化认证版本，密码、角色或启停状态变化后即可让旧会话即时失效。
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai)
    revoked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    user = relationship("PlatformUser", back_populates="sessions")


class Tenant(Base, TimestampMixin):
    """共享数据库中的租户根实体，普通请求的 tenant_id 必须来自可信会话。"""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_tenants_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    contact_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    users = relationship("TenantUser", back_populates="tenant", cascade="all, delete-orphan")
    sessions = relationship("TenantSession", back_populates="tenant", cascade="all, delete-orphan")
    subscriptions = relationship("TenantSubscription", back_populates="tenant", cascade="all, delete-orphan")
    provider_nodes = relationship("ProviderNode", back_populates="tenant", cascade="all, delete-orphan")


class SaasPlan(Base, TimestampMixin):
    """平台套餐；首期仅把 Provider 节点数作为可计费额度。"""

    __tablename__ = "saas_plans"
    __table_args__ = (
        CheckConstraint("provider_node_limit >= 0", name="ck_saas_plans_provider_node_limit"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_saas_plans_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_node_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscriptions = relationship("TenantSubscription", back_populates="plan")


class TenantSubscription(Base, TimestampMixin):
    """记录租户套餐有效期；保留历史记录，当前订阅由状态和时间共同判定。"""

    __tablename__ = "tenant_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'expired', 'suspended')",
            name="ck_tenant_subscriptions_status",
        ),
        UniqueConstraint("tenant_id", "starts_at", name="uq_tenant_subscription_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("saas_plans.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    starts_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai)
    # expires_at 为空表示人工维护的长期有效订阅；过期判断必须统一在服务层完成。
    expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant = relationship("Tenant", back_populates="subscriptions")
    plan = relationship("SaasPlan", back_populates="subscriptions")


class TenantUser(Base, TimestampMixin):
    """租户员工账号；登录名只在当前租户内唯一。"""

    __tablename__ = "tenant_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_tenant_user_username"),
        CheckConstraint(
            "role IN ('tenant_admin', 'operator', 'readonly')",
            name="ck_tenant_users_role",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="readonly", index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    tenant = relationship("Tenant", back_populates="users")
    sessions = relationship("TenantSession", back_populates="user", cascade="all, delete-orphan")


class TenantSession(Base, TimestampMixin):
    """租户员工数据库会话，tenant_id 用于每次请求快速恢复可信租户上下文。"""

    __tablename__ = "tenant_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    tenant_user_id: Mapped[int] = mapped_column(ForeignKey("tenant_users.id"), nullable=False, index=True)
    session_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_seen_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai)
    # 吊销采用时间戳而不是删除记录，便于审计会话失效原因且避免令牌被重复创建。
    revoked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant = relationship("Tenant", back_populates="sessions")
    user = relationship("TenantUser", back_populates="sessions")


class PlatformAuditLog(Base, TimestampMixin):
    """平台级管理审计，与后续增加 tenant_id 的租户内审计表分开保存。"""

    __tablename__ = "platform_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_users.id"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target_tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id"),
        nullable=True,
        index=True,
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    actor = relationship("PlatformUser")
    target_tenant = relationship("Tenant")


class ProviderNode(Base, TenantOwnedMixin, TimestampMixin):
    """客户电脑上的 Provider 安装实例，一个节点可以承载多个企微账号。"""

    __tablename__ = "provider_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_provider_nodes_tenant_name"),
        CheckConstraint("status IN ('pending', 'active', 'disabled')", name="ck_provider_nodes_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending", index=True)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)
    client_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_heartbeat_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    activated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant = relationship("Tenant", back_populates="provider_nodes")
    accounts = relationship("ProviderNodeAccount", back_populates="node", cascade="all, delete-orphan")
    activation_codes = relationship("ProviderActivationCode", back_populates="node", cascade="all, delete-orphan")
    credentials = relationship("ProviderCredential", back_populates="node", cascade="all, delete-orphan")
    heartbeats = relationship("ProviderHeartbeat", back_populates="node", cascade="all, delete-orphan")


class ProviderNodeAccount(Base, TenantOwnedMixin, TimestampMixin):
    """Provider 心跳发现的节点内企微账号，绑定渠道前仅作为候选资源。"""

    __tablename__ = "provider_node_accounts"
    __table_args__ = (
        UniqueConstraint("provider_node_id", "account_key", name="uq_provider_node_account_key"),
        CheckConstraint("status IN ('online', 'offline', 'unknown')", name="ck_provider_node_accounts_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_node_id: Mapped[int] = mapped_column(ForeignKey("provider_nodes.id"), nullable=False, index=True)
    account_key: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    external_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown", index=True)
    last_seen_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    node = relationship("ProviderNode", back_populates="accounts")


class ProviderActivationCode(Base, TenantOwnedMixin, TimestampMixin):
    """一次性节点激活码；数据库只保存高熵原文的不可逆摘要。"""

    __tablename__ = "provider_activation_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_node_id: Mapped[int] = mapped_column(ForeignKey("provider_nodes.id"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_users.id"), nullable=True, index=True)

    node = relationship("ProviderNode", back_populates="activation_codes")


class ProviderCredential(Base, TenantOwnedMixin, TimestampMixin):
    """节点长期设备凭证；客户端持有原始 token，云端只保存摘要。"""

    __tablename__ = "provider_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_node_id: Mapped[int] = mapped_column(ForeignKey("provider_nodes.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    device_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    issued_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai)
    last_used_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revoked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    node = relationship("ProviderNode", back_populates="credentials")


class ProviderHeartbeat(Base, TenantOwnedMixin):
    """节点心跳审计快照，详细运行状态使用 JSON 保存并限制保留数量。"""

    __tablename__ = "provider_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_node_id: Mapped[int] = mapped_column(ForeignKey("provider_nodes.id"), nullable=False, index=True)
    reported_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai, index=True)
    client_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    hook_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    workwechat_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    accounts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    node = relationship("ProviderNode", back_populates="heartbeats")
