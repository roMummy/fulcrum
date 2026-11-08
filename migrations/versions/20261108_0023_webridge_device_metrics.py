"""微桥手机状态采样增加电量与内存指标。

Revision ID: 20261108_0023
Revises: 20261108_0022
Create Date: 2026-11-08 23:40:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261108_0023"
down_revision: Union[str, Sequence[str], None] = "20261108_0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增三个可空指标列，已有采样行保持为空。"""

    with op.batch_alter_table("webridge_device_samples", schema=None) as batch_op:
        batch_op.add_column(sa.Column("battery_percent", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("memory_used_percent", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("process_memory_mb", sa.Integer(), nullable=True))


def downgrade() -> None:
    """删除新增指标列。"""

    with op.batch_alter_table("webridge_device_samples", schema=None) as batch_op:
        batch_op.drop_column("process_memory_mb")
        batch_op.drop_column("memory_used_percent")
        batch_op.drop_column("battery_percent")
