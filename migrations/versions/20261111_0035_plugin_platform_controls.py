"""插件平台管控:plugins 新增平台状态/可见范围/缺失标记,新增 plugin_grants 授权名单。

Revision ID: 20261111_0035
Revises: 20261111_0034
Create Date: 2026-11-11 16:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261111_0035"
down_revision: Union[str, Sequence[str], None] = "20261111_0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """已有插件全部是 active + 所有租户可见,升级后行为不变;之后由平台后台调整。"""

    with op.batch_alter_table("plugins", schema=None) as batch_op:
        batch_op.add_column(sa.Column("platform_status", sa.String(length=20), nullable=False, server_default="active"))
        batch_op.add_column(sa.Column("visibility", sa.String(length=20), nullable=False, server_default="all"))
        batch_op.add_column(sa.Column("status_note", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("missing_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_plugins_platform_status"), ["platform_status"], unique=False)
    op.create_table(
        "plugin_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.Integer(), nullable=False),
        sa.Column("scope_type", sa.String(length=10), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plugin_id"], ["plugins.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "scope_type", "scope_id", name="uq_plugin_grants_scope"),
    )
    with op.batch_alter_table("plugin_grants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plugin_grants_plugin_id"), ["plugin_id"], unique=False)


def downgrade() -> None:
    """删除授权表和新增的列;平台已设置的停用/可见范围会丢失,之后所有插件回到全部可见。"""

    op.drop_table("plugin_grants")
    with op.batch_alter_table("plugins", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_plugins_platform_status"))
        batch_op.drop_column("missing_at")
        batch_op.drop_column("status_note")
        batch_op.drop_column("visibility")
        batch_op.drop_column("platform_status")
