"""新增 AI 核心调度的租户配置表与决策记录表。

Revision ID: 20261109_0024
Revises: 20261108_0023
Create Date: 2026-11-09 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261109_0024"
down_revision: Union[str, Sequence[str], None] = "20261108_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """仅新增两张表，不改动已有数据。"""

    op.create_table(
        "tenant_ai_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("api_base", sa.String(length=255), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_ai_settings_tenant"),
    )
    with op.batch_alter_table("tenant_ai_settings", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tenant_ai_settings_tenant_id"), ["tenant_id"], unique=False)

    op.create_table(
        "ai_decisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("sender_id", sa.String(length=128), nullable=True),
        sa.Column("input_text", sa.String(length=500), nullable=False),
        sa.Column("plugin_code", sa.String(length=100), nullable=True),
        sa.Column("command_text", sa.String(length=300), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ai_decisions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ai_decisions_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_decisions_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_decisions_conversation_id"), ["conversation_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_decisions_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_decisions_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    """删除两张 AI 表；决策记录仅用于复盘，不需要保留。"""

    op.drop_table("ai_decisions")
    op.drop_table("tenant_ai_settings")
