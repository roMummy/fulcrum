from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import now_shanghai
from app.core.db import Base
from app.models.mixins import TenantOwnedMixin, TimestampMixin


class Bot(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "bots"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_bots_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # SaaS 租户只需要一个可识别的业务名称。内部关联统一使用主键，避免再维护
    # slug、分组、模板、标签和环境等与企业微信接入无关的第二套资产体系。
    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="stopped")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    channels = relationship("Channel", back_populates="bot", cascade="all, delete-orphan")
    plugin_bindings = relationship("BotPlugin", back_populates="bot", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="bot", cascade="all, delete-orphan")
    # 商业额度绑定与业务机器人保持一对一关系。旧绑定允许暂时没有关联，
    # 新的统一账号接入流程会在同一事务中写入真实机器人和主渠道。
    entitlement_binding = relationship("BotBinding", back_populates="bot", uselist=False)


class Channel(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "channels"
    __table_args__ = (
        # 平台大盘按租户汇总启用渠道及通用运行、登录状态，组合索引避免逐列索引回表扫描。
        Index("ix_channels_platform_health", "tenant_id", "is_enabled", "status", "login_state"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    channel_type: Mapped[str] = mapped_column(String(50), index=True)
    api_base: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bot_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    bot_token_masked: Mapped[str | None] = mapped_column(String(255), nullable=True)
    webhook_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    dm_policy: Mapped[str] = mapped_column(String(30), default="allowlist")
    allow_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    adapter_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    login_state: Mapped[str] = mapped_column(String(30), default="unknown")
    qrcode_status: Mapped[str] = mapped_column(String(30), default="idle")
    last_login_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_sync_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_info_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="offline")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 逻辑删除时间:非空表示账号已被删除。数据(群、会话、消息)全部保留,但账号从清单隐藏、
    # 不再接收事件和手机请求,占用的额度已释放。
    deleted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # 节点与发现账号绑定由服务端校验租户和父子关系，客户端任务不能临时改选账号。
    provider_node_id: Mapped[int | None] = mapped_column(ForeignKey("provider_nodes.id"), nullable=True, index=True)
    provider_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_node_accounts.id"),
        nullable=True,
        index=True,
    )

    bot = relationship("Bot", back_populates="channels")
    chatrooms = relationship("Chatroom", back_populates="channel", cascade="all, delete-orphan")
    provider_node = relationship("ProviderNode")
    provider_account = relationship("ProviderNodeAccount")
    # 一个正式接入账号只能占用一个主渠道，避免额度记录与实际运行渠道脱节。
    entitlement_binding = relationship("BotBinding", back_populates="channel", uselist=False)


class Plugin(Base, TimestampMixin):
    __tablename__ = "plugins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    plugin_type: Mapped[str] = mapped_column(String(50), default="message")
    version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    author: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    bot_bindings = relationship("BotPlugin", back_populates="plugin", cascade="all, delete-orphan")


class BotPlugin(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "bot_plugins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    plugin_id: Mapped[int] = mapped_column(ForeignKey("plugins.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="enabled")
    runtime_status: Mapped[str] = mapped_column(String(20), default="idle")
    last_reload_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_execution_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_execution_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_execution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime_details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 普通配置继续保存在 config_json；API Key、Token 和密码等敏感字段整体加密后
    # 单独保存，运行时只在可信租户和绑定上下文中短暂解密并合并。
    secret_config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_config_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    bot = relationship("Bot", back_populates="plugin_bindings")
    plugin = relationship("Plugin", back_populates="bot_bindings")
    state_records = relationship("PluginStateRecord", back_populates="binding", cascade="all, delete-orphan")


class PluginStateRecord(Base, TenantOwnedMixin, TimestampMixin):
    """保存租户插件的小型运行状态，不承载独立业务域的事务数据。"""

    __tablename__ = "plugin_state_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "namespace",
            "state_key",
            name="uq_plugin_state_records_locator",
        ),
        Index(
            "ix_plugin_state_records_binding_namespace",
            "tenant_id",
            "bot_plugin_id",
            "namespace",
        ),
        Index("ix_plugin_state_records_expiry", "tenant_id", "expires_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_plugin_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"), nullable=False, index=True)
    namespace: Mapped[str] = mapped_column(String(80), nullable=False)
    state_key: Mapped[str] = mapped_column(String(191), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    binding = relationship("BotPlugin", back_populates="state_records")


class Conversation(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    channel_type: Mapped[str] = mapped_column(String(50), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chat_type: Mapped[str] = mapped_column(String(20), default="direct")
    last_message_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bot = relationship("Bot", back_populates="conversations")
    channel = relationship("Channel")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Chatroom(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "chatrooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending_sync", index=True)
    registration_source: Mapped[str] = mapped_column(String(20), default="passive", index=True)
    management_status: Mapped[str] = mapped_column(String(30), default="discovered", index=True)
    is_registered: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    bot = relationship("Bot")
    channel = relationship("Channel", back_populates="chatrooms")
    members = relationship("ChatroomMember", back_populates="chatroom", cascade="all, delete-orphan")


class ChatroomMember(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "chatroom_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chatroom_id: Mapped[int] = mapped_column(ForeignKey("chatrooms.id"), index=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    member_external_id: Mapped[str] = mapped_column(String(128), index=True)
    nickname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    last_synced_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_active_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    chatroom = relationship("Chatroom", back_populates="members")
    bot = relationship("Bot")
    channel = relationship("Channel")


class WorkWechatContact(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "workwechat_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    contact_kind: Mapped[str] = mapped_column(String(30), default="unknown", index=True)
    contact_external_id: Mapped[str] = mapped_column(String(128), index=True)
    conversation_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    external_userid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    userid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    open_userid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unionid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    alias: Mapped[str | None] = mapped_column(String(128), nullable=True)
    english_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender: Mapped[str] = mapped_column(String(20), default="unknown")
    contact_type: Mapped[str] = mapped_column(String(30), default="unknown")
    corp_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    corp_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    corp_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mobile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    biz_mail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telephone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    department_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    main_department_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_leader_in_dept_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    direct_leader_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    hide_mobile: Mapped[bool] = mapped_column(Boolean, default=False)
    enable: Mapped[bool] = mapped_column(Boolean, default=True)
    member_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    external_profile_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    extattr_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_contact_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="manual", index=True)
    sync_status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    first_seen_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delete_event_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    channel = relationship("Channel")
    relations = relationship("WorkWechatContactRelation", back_populates="contact", cascade="all, delete-orphan")


class WorkWechatContactRelation(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "workwechat_contact_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("workwechat_contacts.id"), index=True)
    owner_userid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    relationship_status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    delete_event_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    remark_corp_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    remark_mobiles_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    add_way: Mapped[str | None] = mapped_column(String(50), nullable=True)
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    oper_userid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    createtime: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    raw_follow_user_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    channel = relationship("Channel")
    contact = relationship("WorkWechatContact", back_populates="relations")


class CustomerOperationRequest(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "customer_operation_requests"
    __table_args__ = (
        # request_id 只保证同一租户、同一企微渠道内幂等，不允许一个租户的请求
        # 占用另一个租户可独立使用的业务请求编号。
        UniqueConstraint(
            "tenant_id",
            "channel_id",
            "request_id",
            name="uq_customer_operation_request_tenant_channel_request",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("workwechat_contacts.id"), nullable=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True, index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("task_jobs.id"), nullable=True, index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True, index=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    scene: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    operation_type: Mapped[str] = mapped_column(String(50), default="send_message", index=True)
    content_type: Mapped[str] = mapped_column(String(50), default="text", index=True)
    target_contact_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    target_external_userid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    target_userid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    biz_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    adapter_result_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    channel = relationship("Channel")
    contact = relationship("WorkWechatContact")
    conversation = relationship("Conversation")
    job = relationship("TaskJob")
    message = relationship("Message")


class OpenApiApp(Base, TenantOwnedMixin, TimestampMixin):
    """开放接口接入应用。

    MBA 作为消息网关时，外部业务系统通过 app_key/app_secret 调用发送接口。
    app_secret 使用可解密密文保存，便于服务端校验 HMAC 签名；同时保存哈希用于后台展示和审计。
    """

    __tablename__ = "open_api_apps"
    __table_args__ = (UniqueConstraint("tenant_id", "app_key", name="uq_open_api_apps_tenant_app_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    app_key: Mapped[str] = mapped_column(String(100), index=True)
    app_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    app_secret_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), nullable=True, index=True)
    default_channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    allowed_plugin_codes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(Text, nullable=True)

    bot = relationship("Bot")
    default_channel = relationship("Channel")


class OpenApiAppChannel(Base, TenantOwnedMixin, TimestampMixin):
    """开放接口应用的渠道授权。

    一个业务系统通常代表一个客户公司；同一家公司可能有多个企业微信号，
    因此不能只靠 OpenApiApp.default_channel_id 限制为单渠道。
    """

    __tablename__ = "open_api_app_channels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", "channel_id", name="uq_open_api_app_tenant_channel"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("open_api_apps.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)

    app = relationship("OpenApiApp")
    channel = relationship("Channel")


class OpenApiUsageRecord(Base, TenantOwnedMixin, TimestampMixin):
    """开放接口月度额度流水。

    每一次额度预扣都保留一条不可覆盖的业务记录，状态从 ``reserved`` 转为
    ``committed`` 或 ``refunded``。reservation_key 是服务端拼接的幂等键，
    让网络重试只命中原记录而不会重复消耗额度。
    """

    __tablename__ = "open_api_usage_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('reserved', 'committed', 'refunded')",
            name="ck_open_api_usage_records_status",
        ),
        CheckConstraint("units > 0", name="ck_open_api_usage_records_units"),
        UniqueConstraint("reservation_key", name="uq_open_api_usage_records_reservation_key"),
        Index(
            "ix_open_api_usage_records_period_metric",
            "tenant_id",
            "period",
            "metric",
            "status",
        ),
        Index(
            "ix_open_api_usage_records_app_period",
            "app_id",
            "period",
            "metric",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("open_api_apps.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    metric: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(191), nullable=False)
    reservation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="reserved", index=True)
    finalized_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    app = relationship("OpenApiApp")


class GroupPluginScope(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "group_plugin_scopes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    chatroom_id: Mapped[int] = mapped_column(ForeignKey("chatrooms.id"), index=True)
    bot_plugin_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"), index=True)
    scope_status: Mapped[str] = mapped_column(String(30), default="inherited", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    action_scope: Mapped[str | None] = mapped_column(String(50), nullable=True)
    policy_effect: Mapped[str | None] = mapped_column(String(20), nullable=True)
    config_override_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_operator_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_operated_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    audit_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    channel = relationship("Channel")
    chatroom = relationship("Chatroom")
    bot_plugin = relationship("BotPlugin")


class RawWebhookEvent(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "raw_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    event_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="received")
    dispatch_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    channel = relationship("Channel")


class Message(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        # 平台分析只扫描有限时间窗口，并按方向、租户和发送者完成消息及触发用户统计。
        Index("ix_messages_platform_analytics", "created_at", "direction", "tenant_id", "sender_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[str] = mapped_column(String(10), index=True)
    sender_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(30), default="text")
    external_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    external_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    raw_request_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="sent")

    conversation = relationship("Conversation", back_populates="messages")


class MessageAttachment(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "message_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), index=True, nullable=True)
    raw_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_webhook_events.id"), index=True, nullable=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=True)
    direction: Mapped[str] = mapped_column(String(10), default="in", index=True)
    attachment_type: Mapped[str] = mapped_column(String(30), default="image", index=True)
    provider: Mapped[str] = mapped_column(String(50), default="unknown", index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    message = relationship("Message")
    raw_event = relationship("RawWebhookEvent")
    channel = relationship("Channel")
    conversation = relationship("Conversation")


class TaskJob(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "task_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_task_jobs_tenant_dedupe_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, index=True)
    started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    related_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    related_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # 只有需要强幂等的任务写入该键；NULL 不参与唯一冲突，现有普通队列保持原行为。
    dedupe_key: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_node_id: Mapped[int | None] = mapped_column(ForeignKey("provider_nodes.id"), nullable=True, index=True)
    provider_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_node_accounts.id"),
        nullable=True,
        index=True,
    )
    lease_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    claimed_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    provider_node = relationship("ProviderNode")
    provider_account = relationship("ProviderNodeAccount")


class WeatherSubscription(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "weather_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=True)
    chat_type: Mapped[str] = mapped_column(String(20), default="group", index=True)
    target_external_id: Mapped[str] = mapped_column(String(128), index=True)
    subscriber_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    city_query: Mapped[str] = mapped_column(String(255))
    city_name: Mapped[str] = mapped_column(String(255))
    city_id: Mapped[str] = mapped_column(String(64), index=True)
    schedule_hour: Mapped[int] = mapped_column(Integer, default=8)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    next_push_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_push_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    bot = relationship("Bot")
    channel = relationship("Channel")
    conversation = relationship("Conversation")


class SignInRecord(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "sign_in_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    total_sign_count: Mapped[int] = mapped_column(Integer, default=0)
    signin_streak: Mapped[int] = mapped_column(Integer, default=0)
    previous_streak: Mapped[int] = mapped_column(Integer, default=0)
    sign_stat: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sign_date: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SignInHistory(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "sign_in_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int | None] = mapped_column(ForeignKey("sign_in_records.id"), index=True, nullable=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    signed_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    is_makeup: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    points_delta: Mapped[int] = mapped_column(Integer, default=0)
    streak: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    record = relationship("SignInRecord")


class PointsAccount(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "points_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_points: Mapped[int] = mapped_column(Integer, default=0)


class PointsTransaction(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "points_transactions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_points_transactions_tenant_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    counterparty_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    points_delta: Mapped[int] = mapped_column(Integer, default=0)
    balance_after: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(50), default="other", index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(191), nullable=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PointsPrisonRecord(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "points_prison_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(30), default="imprisoned", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    bailed_out_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ErrorLog(Base, TenantOwnedMixin):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(100), index=True)
    level: Mapped[str] = mapped_column(String(20), default="ERROR")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai)


class PluginExecutionLog(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "plugin_execution_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), index=True, nullable=True)
    raw_event_id: Mapped[int | None] = mapped_column(ForeignKey("raw_webhook_events.id"), index=True, nullable=True)
    plugin_id: Mapped[int | None] = mapped_column(ForeignKey("plugins.id"), index=True, nullable=True)
    bot_plugin_id: Mapped[int | None] = mapped_column(ForeignKey("bot_plugins.id"), index=True, nullable=True)
    plugin_code: Mapped[str] = mapped_column(String(100), index=True)
    channel_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    chat_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    message_type: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    sender_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    conversation_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    attempted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="skipped", index=True)
    consume_policy: Mapped[str | None] = mapped_column(String(30), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    outbound_count: Mapped[int] = mapped_column(Integer, default=0)
    trigger_source: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class MessageActivityHourly(Base, TenantOwnedMixin):
    __tablename__ = "message_activity_hourly"
    __table_args__ = (
        # 全平台趋势先按时间范围过滤，再按租户和消息方向聚合，不依赖原始消息回退。
        Index(
            "ix_message_activity_hourly_platform_analytics",
            "bucket_start",
            "tenant_id",
            "direction",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    channel_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    chat_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(10), index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai)


class PluginActivityHourly(Base, TenantOwnedMixin):
    __tablename__ = "plugin_activity_hourly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    plugin_id: Mapped[int | None] = mapped_column(ForeignKey("plugins.id"), index=True, nullable=True)
    bot_plugin_id: Mapped[int | None] = mapped_column(ForeignKey("bot_plugins.id"), index=True, nullable=True)
    plugin_code: Mapped[str] = mapped_column(String(100), index=True)
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, default=0)
    attempted_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_count: Mapped[int] = mapped_column(Integer, default=0)
    timeout_count: Mapped[int] = mapped_column(Integer, default=0)
    outbound_count: Mapped[int] = mapped_column(Integer, default=0)
    total_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai)


class HealthCheckRecord(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "health_check_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str] = mapped_column(String(50), index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    summary: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertRecord(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(100), index=True)
    level: Mapped[str] = mapped_column(String(20), default="warning", index=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    related_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    related_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    rule_key: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base, TenantOwnedMixin, TimestampMixin):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(100), default="system", index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(50), index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="readonly", index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class WebridgeDeviceSample(Base, TenantOwnedMixin):
    """微桥手机状态采样：每个渠道每分钟最多一行，用于后台的 iPhone 状态趋势图。

    采样点来自 poll 维护和 state 上报；超过保留期的行在写入新采样时顺带清理。
    """

    __tablename__ = "webridge_device_samples"
    __table_args__ = (UniqueConstraint("channel_id", "bucket_start", name="uq_webridge_device_samples_channel_bucket"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    # 分钟级时间桶，同一分钟内重复采样只更新不新增。
    bucket_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    online: Mapped[bool] = mapped_column(Boolean, default=True)
    pending_commands: Mapped[int] = mapped_column(Integer, default=0)
    running_commands: Mapped[int] = mapped_column(Integer, default=0)
    # 以下两项来自手机 state 上报；手机尚未上报 state 时为 NULL，图表据此跳过。
    cached_message_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_event_queue_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 以下来自手机 device_status 命令（见微桥文档第 17 节）；命令未回报时为 NULL。
    battery_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_used_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    process_memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TenantAiSetting(Base, TenantOwnedMixin, TimestampMixin):
    """租户的大模型配置：AI 是平台核心能力，每个租户只有一份，成本由租户自担。"""

    __tablename__ = "tenant_ai_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_tenant_ai_settings_tenant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 总开关；关闭后所有群都不会触发 AI 调度，规则触发的插件不受影响。
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    api_base: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    # 密钥经 encrypt_secret 加密保存，页面只展示是否已配置。
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 每日最多调用模型的次数，防止群里刷屏造成费用失控。
    daily_limit: Mapped[int] = mapped_column(Integer, default=200)
    # 聊天兜底开关：没有插件命中且不是功能意图时，由模型按人格直接聊天。
    chat_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 租户自定义的人格设定（系统提示），留空使用平台默认人格。
    persona: Mapped[str] = mapped_column(Text, default="")


class AiMemory(Base, TenantOwnedMixin):
    """AI 聊天的长期记忆：按渠道和发言人保存模型提取出的单条事实，如喜好、称呼、近况。"""

    __tablename__ = "ai_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 记忆归属的发言人；同一个人在不同群里共享记忆，注入提示时只带这个人自己的记忆。
    sender_id: Mapped[str] = mapped_column(String(128), index=True)
    # 单行短文本，写入前已清洗并限制长度。
    content: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, index=True)


class AiDecision(Base, TenantOwnedMixin):
    """AI 调度决策记录：谁说了什么、AI 理解成什么、结果如何，用于复盘和额度统计。"""

    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sender_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 输入只保存截断后的摘要，不保存模型密钥或完整上下文。
    input_text: Mapped[str] = mapped_column(String(500), default="")
    plugin_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    command_text: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # routed 已改写并交回调度；asked 缺参数已反问；no_match 模型认为无匹配；
    # low_confidence 置信度不足；rejected 模型输出不在目录内；error 调用失败。
    status: Mapped[str] = mapped_column(String(30), index=True)
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, index=True)
