"""增加租户趣味命令规则表。

Revision ID: 20260901_0018
Revises: 20260901_0017
Create Date: 2026-09-01 04:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0018"
down_revision: Union[str, Sequence[str], None] = "20260901_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建只归属于当前租户机器人插件绑定的趣味命令规则。"""

    op.create_table(
        "fun_command_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_plugin_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("trigger_text", sa.String(length=255), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False),
        sa.Column("replies_json", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "match_type IN ('exact', 'prefix', 'contains')",
            name="ck_fun_command_rules_match_type",
        ),
        sa.CheckConstraint(
            "cooldown_seconds >= 0 AND cooldown_seconds <= 86400",
            name="ck_fun_command_rules_cooldown",
        ),
        sa.ForeignKeyConstraint(["bot_plugin_id"], ["bot_plugins.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "trigger_text",
            name="uq_fun_command_rules_binding_trigger",
        ),
    )
    op.create_index(
        "ix_fun_command_rules_binding_enabled_priority",
        "fun_command_rules",
        ["tenant_id", "bot_plugin_id", "is_enabled", "priority"],
        unique=False,
    )
    op.create_index(
        "ix_fun_command_rules_bot_plugin_id",
        "fun_command_rules",
        ["bot_plugin_id"],
        unique=False,
    )
    op.create_index(
        "ix_fun_command_rules_is_enabled",
        "fun_command_rules",
        ["is_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_fun_command_rules_tenant_id",
        "fun_command_rules",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """删除趣味命令规则表。"""

    op.drop_table("fun_command_rules")
