from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.time import now_shanghai
from app.core.db import Base


class TimestampMixin:
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai)


class Bot(Base, TimestampMixin):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="stopped")
    command_prefix: Mapped[str] = mapped_column(String(32), default="/")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("bot_groups.id"), index=True, nullable=True)
    config_template_id: Mapped[int | None] = mapped_column(ForeignKey("config_templates.id"), index=True, nullable=True)
    environment: Mapped[str] = mapped_column(String(20), default="prod", index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    group = relationship("BotGroup", back_populates="bots")
    config_template = relationship("ConfigTemplate", back_populates="bots")
    channels = relationship("Channel", back_populates="bot", cascade="all, delete-orphan")
    plugin_bindings = relationship("BotPlugin", back_populates="bot", cascade="all, delete-orphan")
    conversations = relationship("Conversation", back_populates="bot", cascade="all, delete-orphan")
    tag_links = relationship("BotTagLink", back_populates="bot", cascade="all, delete-orphan")


class BotGroup(Base, TimestampMixin):
    __tablename__ = "bot_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    bots = relationship("Bot", back_populates="group")


class ConfigTemplate(Base, TimestampMixin):
    __tablename__ = "config_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    template_scope: Mapped[str] = mapped_column(String(30), default="bot")
    channel_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    environment: Mapped[str] = mapped_column(String(20), default="shared", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    bots = relationship("Bot", back_populates="config_template")


class BotTag(Base, TimestampMixin):
    __tablename__ = "bot_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True, index=True)

    bot_links = relationship("BotTagLink", back_populates="tag", cascade="all, delete-orphan")


class BotTagLink(Base, TimestampMixin):
    __tablename__ = "bot_tag_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("bot_tags.id"), index=True)

    bot = relationship("Bot", back_populates="tag_links")
    tag = relationship("BotTag", back_populates="bot_links")


class Channel(Base, TimestampMixin):
    __tablename__ = "channels"

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

    bot = relationship("Bot", back_populates="channels")
    chatrooms = relationship("Chatroom", back_populates="channel", cascade="all, delete-orphan")


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


class BotPlugin(Base, TimestampMixin):
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

    bot = relationship("Bot", back_populates="plugin_bindings")
    plugin = relationship("Plugin", back_populates="bot_bindings")


class Conversation(Base, TimestampMixin):
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


class Chatroom(Base, TimestampMixin):
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


class ChatroomMember(Base, TimestampMixin):
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


class WorkWechatContact(Base, TimestampMixin):
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


class WorkWechatContactRelation(Base, TimestampMixin):
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


class CustomerOperationRequest(Base, TimestampMixin):
    __tablename__ = "customer_operation_requests"
    __table_args__ = (
        UniqueConstraint("channel_id", "request_id", name="uq_customer_operation_request_channel_request"),
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


class OpenApiApp(Base, TimestampMixin):
    """开放接口接入应用。

    MBA 作为消息网关时，外部业务系统通过 app_key/app_secret 调用发送接口。
    app_secret 使用可解密密文保存，便于服务端校验 HMAC 签名；同时保存哈希用于后台展示和审计。
    """

    __tablename__ = "open_api_apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    app_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
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


class OpenApiAppChannel(Base, TimestampMixin):
    """开放接口应用的渠道授权。

    一个业务系统通常代表一个客户公司；同一家公司可能有多个企业微信号，
    因此不能只靠 OpenApiApp.default_channel_id 限制为单渠道。
    """

    __tablename__ = "open_api_app_channels"
    __table_args__ = (UniqueConstraint("app_id", "channel_id", name="uq_open_api_app_channel"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("open_api_apps.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)

    app = relationship("OpenApiApp")
    channel = relationship("Channel")


class GroupPluginScope(Base, TimestampMixin):
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


class RawWebhookEvent(Base, TimestampMixin):
    __tablename__ = "raw_webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    event_key: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="received")
    dispatch_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    channel = relationship("Channel")


class Message(Base, TimestampMixin):
    __tablename__ = "messages"

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


class MessageAttachment(Base, TimestampMixin):
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


class TaskJob(Base, TimestampMixin):
    __tablename__ = "task_jobs"

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
    payload_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class WeatherSubscription(Base, TimestampMixin):
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


class SignInRecord(Base, TimestampMixin):
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


class SignInHistory(Base, TimestampMixin):
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


class PointsAccount(Base, TimestampMixin):
    __tablename__ = "points_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_points: Mapped[int] = mapped_column(Integer, default=0)


class PointsTransaction(Base, TimestampMixin):
    __tablename__ = "points_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    counterparty_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    points_delta: Mapped[int] = mapped_column(Integer, default=0)
    balance_after: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(50), default="other", index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class PointsPrisonRecord(Base, TimestampMixin):
    __tablename__ = "points_prison_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    group_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    status: Mapped[str] = mapped_column(String(30), default="imprisoned", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    bailed_out_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ErrorLog(Base):
    __tablename__ = "error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(100), index=True)
    level: Mapped[str] = mapped_column(String(20), default="ERROR")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai)


class PluginExecutionLog(Base, TimestampMixin):
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


class MessageActivityHourly(Base):
    __tablename__ = "message_activity_hourly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    environment: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    bot_id: Mapped[int | None] = mapped_column(ForeignKey("bots.id"), index=True, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), index=True, nullable=True)
    channel_type: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    chat_type: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(10), index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai)


class PluginActivityHourly(Base):
    __tablename__ = "plugin_activity_hourly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_start: Mapped[DateTime] = mapped_column(DateTime(timezone=True), index=True)
    environment: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
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


class HealthCheckRecord(Base, TimestampMixin):
    __tablename__ = "health_check_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(50), index=True)
    target_type: Mapped[str] = mapped_column(String(50), index=True)
    target_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    summary: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlertRecord(Base, TimestampMixin):
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


class AuditLog(Base, TimestampMixin):
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
