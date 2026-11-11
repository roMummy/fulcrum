"""新增群级触发回复规则表 group_trigger_rules。

Revision ID: 20261111_0036
Revises: 20261111_0035
Create Date: 2026-11-11 18:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261111_0036"
down_revision: Union[str, Sequence[str], None] = "20261111_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """只新增一张表,不改动任何已有数据。"""

    op.create_table(
        "group_trigger_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("chatroom_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("trigger_json", sa.Text(), nullable=False),
        sa.Column("actions_json", sa.Text(), nullable=False),
        sa.Column("pick_mode", sa.String(length=10), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("pick_mode IN ('all', 'random')", name="ck_group_trigger_rules_pick_mode"),
        sa.CheckConstraint("cooldown_seconds >= 0 AND cooldown_seconds <= 86400", name="ck_group_trigger_rules_cooldown"),
        sa.ForeignKeyConstraint(["chatroom_id"], ["chatrooms.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_group_trigger_rules_chatroom_id", "group_trigger_rules", ["chatroom_id"], unique=False)
    op.create_index("ix_group_trigger_rules_is_enabled", "group_trigger_rules", ["is_enabled"], unique=False)
    op.create_index("ix_group_trigger_rules_tenant_id", "group_trigger_rules", ["tenant_id"], unique=False)
    op.create_index(
        "ix_group_trigger_rules_group_enabled",
        "group_trigger_rules",
        ["tenant_id", "chatroom_id", "is_enabled", "priority"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("group_trigger_rules")
