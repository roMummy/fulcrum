"""增加 NewAPI 领券账号绑定表。

Revision ID: 20260901_0017
Revises: 20260831_0016
Create Date: 2026-09-01 01:45:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0017"
down_revision: Union[str, Sequence[str], None] = "20260831_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建租户归属的领取预留和最终账号绑定记录。"""

    op.create_table(
        "coupon_account_bindings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_plugin_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("member_external_id", sa.String(length=128), nullable=False),
        sa.Column("email_normalized", sa.String(length=254), nullable=False),
        sa.Column("newapi_user_id", sa.Integer(), nullable=True),
        sa.Column("newapi_username", sa.String(length=64), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column("plan_title", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("notification_status", sa.String(length=20), nullable=False),
        sa.Column("last_message_id", sa.Integer(), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'failed')",
            name="ck_coupon_account_bindings_status",
        ),
        sa.CheckConstraint(
            "notification_status IN ('pending', 'sent', 'failed', 'not_required')",
            name="ck_coupon_account_bindings_notification_status",
        ),
        sa.ForeignKeyConstraint(["bot_plugin_id"], ["bot_plugins.id"]),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["last_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "channel_id",
            "member_external_id",
            name="uq_coupon_binding_tenant_channel_member",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "email_normalized",
            name="uq_coupon_binding_tenant_email",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "newapi_user_id",
            name="uq_coupon_binding_tenant_newapi_user",
        ),
    )
    for column_name in (
        "tenant_id",
        "bot_plugin_id",
        "channel_id",
        "conversation_id",
        "member_external_id",
        "email_normalized",
        "newapi_user_id",
        "plan_id",
        "status",
        "notification_status",
        "last_message_id",
        "issued_at",
    ):
        op.create_index(
            f"ix_coupon_account_bindings_{column_name}",
            "coupon_account_bindings",
            [column_name],
            unique=False,
        )


def downgrade() -> None:
    """删除 NewAPI 领券账号绑定表。"""

    op.drop_table("coupon_account_bindings")
