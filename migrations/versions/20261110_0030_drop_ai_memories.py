"""删除 ai_memories:AI 长期记忆改由 Hindsight 记忆服务负责。

Revision ID: 20261110_0030
Revises: 20261110_0029
Create Date: 2026-11-10 23:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261110_0030"
down_revision: Union[str, Sequence[str], None] = "20261110_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """旧的数据库记忆(每人最多 30 条短句)不再使用,线上该表为空,直接删除,保持单一实现。"""

    op.drop_table("ai_memories")


def downgrade() -> None:
    """旧实现已废弃,仅恢复空表结构以便回滚迁移链。"""

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
