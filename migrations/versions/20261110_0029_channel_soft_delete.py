"""账号(渠道)逻辑删除:channels 新增 deleted_at。

Revision ID: 20261110_0029
Revises: 20261110_0028
Create Date: 2026-11-10 22:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261110_0029"
down_revision: Union[str, Sequence[str], None] = "20261110_0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增可空的删除时间;已有账号都是未删除状态,无需回填。"""

    with op.batch_alter_table("channels", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_channels_deleted_at"), ["deleted_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("channels", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_channels_deleted_at"))
        batch_op.drop_column("deleted_at")
