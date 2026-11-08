"""新增微桥手机状态采样表。

Revision ID: 20261108_0022
Revises: 20261108_0021
Create Date: 2026-11-08 23:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261108_0022"
down_revision: Union[str, Sequence[str], None] = "20261108_0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建按渠道、分钟唯一的手机状态采样表，仅新增表，不改动已有数据。"""

    op.create_table(
        "webridge_device_samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("online", sa.Boolean(), nullable=False),
        sa.Column("pending_commands", sa.Integer(), nullable=False),
        sa.Column("running_commands", sa.Integer(), nullable=False),
        sa.Column("cached_message_count", sa.Integer(), nullable=True),
        sa.Column("pending_event_queue_count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "bucket_start", name="uq_webridge_device_samples_channel_bucket"),
    )
    with op.batch_alter_table("webridge_device_samples", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_webridge_device_samples_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_webridge_device_samples_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_webridge_device_samples_bucket_start"), ["bucket_start"], unique=False)


def downgrade() -> None:
    """删除采样表；采样数据可由手机重新上报，不需要保留。"""

    op.drop_table("webridge_device_samples")
