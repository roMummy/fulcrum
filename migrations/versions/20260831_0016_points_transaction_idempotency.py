"""增加积分流水租户内幂等键。

Revision ID: 20260831_0016
Revises: 20260831_0015
Create Date: 2026-08-31 22:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0016"
down_revision: Union[str, Sequence[str], None] = "20260831_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增可空幂等键，不改变没有声明业务键的历史积分写入。"""

    with op.batch_alter_table("points_transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=191), nullable=True))
        batch_op.create_index("ix_points_transactions_idempotency_key", ["idempotency_key"], unique=False)
        batch_op.create_unique_constraint(
            "uq_points_transactions_tenant_idempotency",
            ["tenant_id", "idempotency_key"],
        )


def downgrade() -> None:
    """删除积分流水幂等约束和字段。"""

    with op.batch_alter_table("points_transactions", schema=None) as batch_op:
        batch_op.drop_constraint("uq_points_transactions_tenant_idempotency", type_="unique")
        batch_op.drop_index("ix_points_transactions_idempotency_key")
        batch_op.drop_column("idempotency_key")
