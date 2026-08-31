from __future__ import annotations

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
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
