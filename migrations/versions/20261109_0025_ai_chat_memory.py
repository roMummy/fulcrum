"""AI 聊天兜底：租户设置增加人格与聊天开关，新增长期记忆表。

Revision ID: 20261109_0025
Revises: 20261109_0024
Create Date: 2026-11-09 14:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261109_0025"
down_revision: Union[str, Sequence[str], None] = "20261109_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """已有租户的聊天开关默认关闭，升级后行为不变；记忆表为全新表。"""

    with op.batch_alter_table("tenant_ai_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chat_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("persona", sa.Text(), nullable=False, server_default=""))

    op.create_table(
        "ai_memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("sender_id", sa.String(length=128), nullable=False),
        sa.Column("content", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ai_memories", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_ai_memories_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_memories_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_memories_sender_id"), ["sender_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_ai_memories_created_at"), ["created_at"], unique=False)


def downgrade() -> None:
    """删除记忆表和两个新增列。"""

    op.drop_table("ai_memories")
    with op.batch_alter_table("tenant_ai_settings", schema=None) as batch_op:
        batch_op.drop_column("persona")
        batch_op.drop_column("chat_enabled")
