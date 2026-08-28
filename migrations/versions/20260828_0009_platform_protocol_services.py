"""增加平台托管协议服务配置。

Revision ID: 20260828_0009
Revises: 20260827_0008
Create Date: 2026-08-28 18:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0009"
down_revision: Union[str, Sequence[str], None] = "20260827_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建不带租户主键的平台协议服务表，由平台后台首次保存时写入配置。"""

    op.create_table(
        "platform_protocol_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_type", sa.String(length=64), nullable=False),
        sa.Column("api_base", sa.String(length=500), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("platform_protocol_services", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_platform_protocol_services_channel_type"),
            ["channel_type"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_platform_protocol_services_is_enabled"),
            ["is_enabled"],
            unique=False,
        )


def downgrade() -> None:
    """删除平台协议服务配置表，不修改租户渠道中已经同步的服务地址。"""

    with op.batch_alter_table("platform_protocol_services", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_platform_protocol_services_is_enabled"))
        batch_op.drop_index(batch_op.f("ix_platform_protocol_services_channel_type"))
    op.drop_table("platform_protocol_services")
