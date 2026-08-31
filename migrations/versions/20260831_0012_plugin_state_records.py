"""增加租户插件通用状态记录。

Revision ID: 20260831_0012
Revises: 20260831_0011
Create Date: 2026-08-31 20:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0012"
down_revision: Union[str, Sequence[str], None] = "20260831_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建带租户和插件绑定双重定位的小型状态表。"""

    op.create_table(
        "plugin_state_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_plugin_id", sa.Integer(), nullable=False),
        sa.Column("namespace", sa.String(length=80), nullable=False),
        sa.Column("state_key", sa.String(length=191), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bot_plugin_id"], ["bot_plugins.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "namespace",
            "state_key",
            name="uq_plugin_state_records_locator",
        ),
    )
    with op.batch_alter_table("plugin_state_records", schema=None) as batch_op:
        batch_op.create_index("ix_plugin_state_records_bot_plugin_id", ["bot_plugin_id"], unique=False)
        batch_op.create_index("ix_plugin_state_records_expires_at", ["expires_at"], unique=False)
        batch_op.create_index("ix_plugin_state_records_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index(
            "ix_plugin_state_records_binding_namespace",
            ["tenant_id", "bot_plugin_id", "namespace"],
            unique=False,
        )
        batch_op.create_index(
            "ix_plugin_state_records_expiry",
            ["tenant_id", "expires_at"],
            unique=False,
        )


def downgrade() -> None:
    """删除插件状态表及其索引。"""

    with op.batch_alter_table("plugin_state_records", schema=None) as batch_op:
        batch_op.drop_index("ix_plugin_state_records_expiry")
        batch_op.drop_index("ix_plugin_state_records_binding_namespace")
        batch_op.drop_index("ix_plugin_state_records_tenant_id")
        batch_op.drop_index("ix_plugin_state_records_expires_at")
        batch_op.drop_index("ix_plugin_state_records_bot_plugin_id")
    op.drop_table("plugin_state_records")
