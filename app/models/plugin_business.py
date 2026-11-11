from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TenantOwnedMixin, TimestampMixin


class CouponAccountBinding(Base, TenantOwnedMixin, TimestampMixin):
    """保存一个租户插件绑定内不可自行更换的渠道用户、邮箱和 NewAPI 账号关系。"""

    __tablename__ = "coupon_account_bindings"
    __table_args__ = (
        # 渠道外部用户 ID 只在当前渠道内有稳定含义，因此用户唯一键必须带 channel_id。
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "channel_id",
            "member_external_id",
            name="uq_coupon_binding_tenant_channel_member",
        ),
        # 同一租户插件绑定内，一个邮箱和一个 NewAPI 用户都只能发放给一个渠道用户。
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "email_normalized",
            name="uq_coupon_binding_tenant_email",
        ),
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "newapi_user_id",
            name="uq_coupon_binding_tenant_newapi_user",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'failed')",
            name="ck_coupon_account_bindings_status",
        ),
        CheckConstraint(
            "notification_status IN ('pending', 'sent', 'failed', 'not_required')",
            name="ck_coupon_account_bindings_notification_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_plugin_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"), nullable=False, index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    member_external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    email_normalized: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    newapi_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    newapi_username: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    plan_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    notification_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="not_required",
        index=True,
    )
    last_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True, index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    issued_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    bot_plugin = relationship("BotPlugin")
    channel = relationship("Channel")
    conversation = relationship("Conversation")
    last_message = relationship("Message")


class FunCommandRule(Base, TenantOwnedMixin, TimestampMixin):
    """保存一个租户机器人绑定内可直接触发的趣味命令及标准回复动作。"""

    __tablename__ = "fun_command_rules"
    __table_args__ = (
        # 一个机器人只允许维护一条同名触发规则，避免 exact、prefix 等不同匹配方式
        # 对同一文本形成不透明的执行顺序；需要调整行为时应直接修改现有规则。
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "trigger_text",
            name="uq_fun_command_rules_binding_trigger",
        ),
        CheckConstraint(
            "match_type IN ('exact', 'prefix', 'contains')",
            name="ck_fun_command_rules_match_type",
        ),
        CheckConstraint(
            "cooldown_seconds >= 0 AND cooldown_seconds <= 86400",
            name="ck_fun_command_rules_cooldown",
        ),
        Index(
            "ix_fun_command_rules_binding_enabled_priority",
            "tenant_id",
            "bot_plugin_id",
            "is_enabled",
            "priority",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_plugin_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_text: Mapped[str] = mapped_column(String(255), nullable=False)
    match_type: Mapped[str] = mapped_column(String(20), nullable=False, default="exact")
    replies_json: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    bot_plugin = relationship("BotPlugin")


class GroupTriggerRule(Base, TenantOwnedMixin, TimestampMixin):
    """群级「触发 → 回复」规则:某个群里发生某类事件(拍一拍、关键词…)时,按动作列表自动回复。

    触发类型与动作类型都由代码里的注册表定义(app/services/trigger_rules),这里只存用户的选择:
    trigger_json 是触发参数,actions_json 是有序的动作列表,新增触发或动作种类不需要改表。
    """

    __tablename__ = "group_trigger_rules"
    __table_args__ = (
        CheckConstraint("pick_mode IN ('all', 'random')", name="ck_group_trigger_rules_pick_mode"),
        CheckConstraint("cooldown_seconds >= 0 AND cooldown_seconds <= 86400", name="ck_group_trigger_rules_cooldown"),
        Index("ix_group_trigger_rules_group_enabled", "tenant_id", "chatroom_id", "is_enabled", "priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chatroom_id: Mapped[int] = mapped_column(ForeignKey("chatrooms.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    actions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # all:动作按顺序全部发送;random:每次随机发其中一条(「拍一拍随机回一句」这类场景)
    pick_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="all")
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    # 上次触发时间:冷却按「这条规则在这个群里」计算,不是每个人单独计算
    last_triggered_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
