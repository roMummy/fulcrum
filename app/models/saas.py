from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
BILLING_CYCLES = frozenset({"monthly", "yearly"})
BILLING_ORDER_STATUSES = frozenset(
    {"pending_payment", "payment_submitted", "approved", "rejected", "cancelled"}
)
BOT_BINDING_STATUSES = frozenset({"active", "disabled", "released"})
PROXY_HEALTH_STATUSES = frozenset({"healthy", "unhealthy"})
LOGIN_PROXY_MODES = frozenset({"direct", "platform_pool", "custom", "local_connector"})
LOCAL_PROXY_CONNECTOR_STATUSES = frozenset({"pending", "active", "disabled"})
LOCAL_PROXY_CLEANUP_STATES = frozenset({"not_required", "pending", "completed", "failed"})
LOGIN_PROXY_SESSION_STATUSES = frozenset(
    {"preparing", "qrcode_ready", "completed", "failed", "cancelled", "expired"}
)


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
    reviewed_billing_orders = relationship("BillingOrder", back_populates="reviewed_by_user")
    released_bot_bindings = relationship("BotBinding", back_populates="released_by_user")


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


class PlatformProtocolService(Base, TimestampMixin):
    """平台统一维护的协议服务入口，不属于任何租户，也不保存账号级凭证。"""

    __tablename__ = "platform_protocol_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 渠道稳定编码与渠道目录一致，唯一约束保证首期每种协议只有一个平台入口。
    channel_type: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    api_base: Mapped[str] = mapped_column(String(500), nullable=False)
    # 停用只关闭新的账号接入；已有渠道保留最后一次同步的地址，避免维护动作直接中断业务。
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)


class PlatformProxy(Base, TimestampMixin):
    """平台统一维护的 SOCKS 登录代理，不归属于任何单个租户。"""

    __tablename__ = "platform_proxies"
    __table_args__ = (
        UniqueConstraint("host", "port", "username", name="uq_platform_proxy_endpoint"),
        CheckConstraint("port >= 1 AND port <= 65535", name="ck_platform_proxies_port"),
        CheckConstraint("consecutive_failures >= 0", name="ck_platform_proxies_failures"),
        CheckConstraint(
            "health_status IN ('healthy', 'unhealthy')",
            name="ck_platform_proxies_health_status",
        ),
        # 分配先按地区和可用状态过滤，再检查租约是否已经到期；该索引覆盖扫码主查询。
        Index(
            "ix_platform_proxies_allocation",
            "province",
            "city",
            "is_enabled",
            "health_status",
            "lease_expires_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    province: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    city: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    username: Mapped[str] = mapped_column(String(191), nullable=False, default="")
    # 密码需要在调用 874 时解密，因此使用项目统一可解密密文，页面和日志不得读取原文。
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    health_status: Mapped[str] = mapped_column(String(20), nullable=False, default="healthy", index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 租约键由服务端登录会话生成。与会话主键解耦后可以先创建会话再原子占用代理，
    # 同时避免平台代理表和租户会话表形成互相依赖的循环外键。
    lease_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    lease_expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_checked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_assigned_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_result_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)


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
    billing_orders = relationship("BillingOrder", back_populates="tenant", cascade="all, delete-orphan")
    bot_bindings = relationship("BotBinding", back_populates="tenant", cascade="all, delete-orphan")
    device_trial_claims = relationship(
        "DeviceTrialClaim",
        back_populates="tenant",
        cascade="all, delete-orphan",
    )
    provider_nodes = relationship("ProviderNode", back_populates="tenant", cascade="all, delete-orphan")
    legacy_import_batches = relationship("LegacyImportBatch", back_populates="tenant", cascade="all, delete-orphan")


class SaasPlan(Base, TimestampMixin):
    """平台套餐；新商业流程使用通用机器人额度，旧节点额度仅保留兼容。"""

    __tablename__ = "saas_plans"
    __table_args__ = (
        CheckConstraint("provider_node_limit >= 0", name="ck_saas_plans_provider_node_limit"),
        CheckConstraint("bot_limit >= 0", name="ck_saas_plans_bot_limit"),
        CheckConstraint("open_api_app_limit >= 0", name="ck_saas_plans_open_api_app_limit"),
        CheckConstraint("open_api_channel_limit >= 0", name="ck_saas_plans_open_api_channel_limit"),
        CheckConstraint("monthly_api_request_limit >= 0", name="ck_saas_plans_monthly_api_request_limit"),
        CheckConstraint("monthly_message_limit >= 0", name="ck_saas_plans_monthly_message_limit"),
        CheckConstraint("monthly_media_bytes_limit >= 0", name="ck_saas_plans_monthly_media_bytes_limit"),
        CheckConstraint("monthly_price_cents >= 0", name="ck_saas_plans_monthly_price"),
        CheckConstraint("yearly_price_cents >= 0", name="ck_saas_plans_yearly_price"),
        CheckConstraint("trial_days >= 0", name="ck_saas_plans_trial_days"),
        CheckConstraint("display_order >= 0", name="ck_saas_plans_display_order"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_saas_plans_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # provider_node_limit 仍由旧 Provider 管理页面使用；订单、注册和机器人绑定服务不得读取它。
    provider_node_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bot_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 开放接口资源额度独立于旧 Provider 节点和机器人额度；0 表示该项不设上限，
    # 这样历史套餐在迁移后仍能保持原有业务可用性，平台可以在新套餐中显式配置限制。
    open_api_app_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_api_channel_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monthly_api_request_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monthly_message_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monthly_media_bytes_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 金额统一保存为人民币分，禁止业务层用浮点数直接换算或接受客户端提交的订单金额。
    monthly_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    yearly_price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trial_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_public: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    subscriptions = relationship("TenantSubscription", back_populates="plan")
    billing_orders = relationship("BillingOrder", back_populates="plan")


class TenantSubscription(Base, TimestampMixin):
    """记录租户套餐有效期；保留历史记录，当前订阅由状态和时间共同判定。"""

    __tablename__ = "tenant_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'expired', 'suspended')",
            name="ck_tenant_subscriptions_status",
        ),
        UniqueConstraint("tenant_id", "starts_at", name="uq_tenant_subscription_start"),
        # 平台按状态和到期时间识别有效、即将到期及失效订阅，并回到租户与套餐统计。
        Index(
            "ix_tenant_subscriptions_platform_health",
            "status",
            "expires_at",
            "tenant_id",
            "plan_id",
        ),
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
    fulfilled_order = relationship("BillingOrder", back_populates="fulfilled_subscription", uselist=False)


class BillingOrder(Base, TimestampMixin):
    """租户套餐订单；金额和额度均为下单时由服务端固化的不可变快照。"""

    __tablename__ = "billing_orders"
    __table_args__ = (
        CheckConstraint("billing_cycle IN ('monthly', 'yearly')", name="ck_billing_orders_cycle"),
        CheckConstraint("bot_limit_snapshot >= 0", name="ck_billing_orders_bot_limit"),
        CheckConstraint("amount_cents >= 0", name="ck_billing_orders_amount"),
        CheckConstraint(
            "status IN ('pending_payment', 'payment_submitted', 'approved', 'rejected', 'cancelled')",
            name="ck_billing_orders_status",
        ),
        # 经营大盘按订单状态和审核时间统计待办与已审核金额，不读取付款凭证内容。
        Index("ix_billing_orders_platform_revenue", "status", "reviewed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 订单号用于租户与平台共同定位订单，必须高熵生成并在全平台保持唯一。
    order_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("saas_plans.id"), nullable=False, index=True)
    billing_cycle: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    bot_limit_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending_payment", index=True)
    payment_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 这里只保存私有存储中的相对路径。页面、日志和审计记录不得暴露真实服务器路径或凭证正文。
    payment_proof_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payment_submitted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_users.id"),
        nullable=True,
        index=True,
    )
    reviewed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 一张订单最多履约一次；唯一外键同时作为重复审核的数据库兜底。
    fulfilled_subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenant_subscriptions.id"),
        nullable=True,
        unique=True,
        index=True,
    )

    tenant = relationship("Tenant", back_populates="billing_orders")
    plan = relationship("SaasPlan", back_populates="billing_orders")
    reviewed_by_user = relationship("PlatformUser", back_populates="reviewed_billing_orders")
    fulfilled_subscription = relationship("TenantSubscription", back_populates="fulfilled_order")


class BotBinding(Base, TimestampMixin):
    """通用机器人账号绑定，不依赖 Provider 节点或具体渠道模型。"""

    __tablename__ = "bot_bindings"
    __table_args__ = (
        # 首版以数据库唯一约束阻止跨租户重复占用；受控释放通过更新原记录保留审计字段。
        UniqueConstraint("adapter_type", "bot_account_id", name="uq_bot_binding_adapter_account"),
        CheckConstraint(
            "status IN ('active', 'disabled', 'released')",
            name="ck_bot_bindings_status",
        ),
        # 使用漏斗和租户排行按未释放状态、租户及适配类型统计通用接入账号。
        Index("ix_bot_bindings_platform_usage", "status", "tenant_id", "adapter_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    # 旧版手工绑定记录可能没有实际机器人和渠道，因此迁移保持可空；新的账号
    # 接入事务必须同时填写这两个唯一外键，保证一个额度对应一个可运行账号。
    bot_id: Mapped[int | None] = mapped_column(
        ForeignKey("bots.id"),
        nullable=True,
        unique=True,
        index=True,
    )
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id"),
        nullable=True,
        unique=True,
        index=True,
    )
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bot_account_id: Mapped[str] = mapped_column(String(191), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # 原始设备指纹只在请求内参与哈希，数据库、日志、异常和返回值都只能出现此摘要。
    device_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    bound_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai)
    released_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_users.id"),
        nullable=True,
        index=True,
    )
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    tenant = relationship("Tenant", back_populates="bot_bindings")
    bot = relationship("Bot", back_populates="entitlement_binding")
    channel = relationship("Channel", back_populates="entitlement_binding")
    released_by_user = relationship("PlatformUser", back_populates="released_bot_bindings")


class DeviceTrialClaim(Base, TimestampMixin):
    """设备免费试用领取记录；摘要永久保留，解绑和到期均不得删除。"""

    __tablename__ = "device_trial_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 全局唯一摘要是防止批量注册重复领取试用的最终数据库约束。
    device_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    claimed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai)

    tenant = relationship("Tenant", back_populates="device_trial_claims")


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
    # 只有公开注册的企业所有者必须填写手机号；全局唯一约束实现一个手机号对应一个企业。
    mobile: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="readonly", index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    tenant = relationship("Tenant", back_populates="users")
    sessions = relationship("TenantSession", back_populates="user", cascade="all, delete-orphan")


class TenantSession(Base, TimestampMixin):
    """租户员工数据库会话，tenant_id 用于每次请求快速恢复可信租户上下文。"""

    __tablename__ = "tenant_sessions"
    __table_args__ = (
        # 在线员工统计先按最近访问时间过滤，再校验吊销、过期状态并按租户去重。
        Index(
            "ix_tenant_sessions_platform_presence",
            "last_seen_at",
            "revoked_at",
            "expires_at",
            "tenant_id",
        ),
    )

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


class LoginProxySession(Base, TenantOwnedMixin, TimestampMixin):
    """一次扫码登录使用的临时代理会话，终态后立即释放平台代理和自有凭证。"""

    __tablename__ = "login_proxy_sessions"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('direct', 'platform_pool', 'custom', 'local_connector')",
            name="ck_login_proxy_sessions_mode",
        ),
        CheckConstraint(
            "state IN ('preparing', 'qrcode_ready', 'completed', 'failed', 'cancelled', 'expired')",
            name="ck_login_proxy_sessions_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_login_proxy_sessions_attempt_count"),
        CheckConstraint(
            "custom_port IS NULL OR (custom_port >= 1 AND custom_port <= 65535)",
            name="ck_login_proxy_sessions_custom_port",
        ),
        # 页面按渠道读取当前未结束会话；状态和到期时间共同决定记录是否仍能继续扫码。
        Index(
            "ix_login_proxy_sessions_channel_state",
            "tenant_id",
            "channel_id",
            "state",
            "expires_at",
        ),
        CheckConstraint(
            "proxy_cleanup_state IN ('not_required', 'pending', 'completed', 'failed')",
            name="ck_login_proxy_sessions_proxy_cleanup_state",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False, index=True)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    province: Mapped[str | None] = mapped_column(String(64), nullable=True)
    city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    platform_proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_proxies.id"),
        nullable=True,
        index=True,
    )
    proxy_connector_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxy_connectors.id"),
        nullable=True,
        index=True,
    )
    custom_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    custom_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    custom_username: Mapped[str | None] = mapped_column(String(191), nullable=True)
    # 自有代理密码只在本次登录会话有效；终态处理必须主动将此字段清空。
    custom_password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="preparing", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempted_proxy_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    qrcode_expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    relay_username_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    relay_password_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    relay_expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    proxy_cleanup_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_required", index=True
    )
    proxy_cleanup_error: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ProxyConnector(Base, TenantOwnedMixin, TimestampMixin):
    """租户绑定的 Windows 本地连接器；只保存设备和在线状态，不保存本地 SOCKS 凭证。"""

    __tablename__ = "proxy_connectors"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="ck_proxy_connectors_status",
        ),
        UniqueConstraint("tenant_id", "name", name="uq_proxy_connectors_tenant_name"),
        UniqueConstraint("device_id", name="uq_proxy_connectors_device_id"),
        Index("ix_proxy_connectors_tenant_status", "tenant_id", "status", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    device_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    last_seen_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    client_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    local_proxy_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    disabled_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProxyConnectorActivationCode(Base, TimestampMixin):
    """一次性连接器激活码，只保存不可逆摘要。"""

    __tablename__ = "proxy_connector_activation_codes"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_proxy_connector_activation_code_hash"),
        Index("ix_proxy_connector_activation_codes_connector", "connector_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("proxy_connectors.id"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_users.id"), nullable=True)


class ProxyConnectorCredential(Base, TimestampMixin):
    """连接器设备密钥摘要；密钥原文只存在 Windows 连接器受保护存储中。"""

    __tablename__ = "proxy_connector_credentials"
    __table_args__ = (
        UniqueConstraint("device_key_hash", name="uq_proxy_connector_credential_key_hash"),
        Index("ix_proxy_connector_credentials_connector", "connector_id", "revoked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    connector_id: Mapped[int] = mapped_column(ForeignKey("proxy_connectors.id"), nullable=False, index=True)
    device_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_shanghai)
    last_used_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class LegacyImportBatch(Base, TimestampMixin):
    """旧系统导入批次审计；唯一来源指纹用于数据库级阻止重复导入。"""

    __tablename__ = "legacy_import_batches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_fingerprint", name="uq_legacy_import_tenant_source"),
        CheckConstraint("status IN ('completed')", name="ck_legacy_import_batches_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), nullable=False, index=True)
    # 指纹由脱敏来源标识和各核心表的数量/最大主键计算，不能反推出数据库密码。
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed", index=True)
    # 报告只保存逐表数量、警告和错误代码，不保存渠道密钥、任务负载或来源连接串。
    report_json: Mapped[str] = mapped_column(Text, nullable=False)

    tenant = relationship("Tenant", back_populates="legacy_import_batches")


class ProviderNode(Base, TenantOwnedMixin, TimestampMixin):
    """客户电脑上的 Provider 安装实例，一个节点可以承载多个企微账号。"""

    __tablename__ = "provider_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_provider_nodes_tenant_name"),
        CheckConstraint("status IN ('pending', 'active', 'disabled')", name="ck_provider_nodes_status"),
        # 平台在线状态只读取通用节点状态和心跳时间，并按租户汇总活跃执行客户端。
        Index("ix_provider_nodes_platform_presence", "status", "last_heartbeat_at", "tenant_id"),
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
