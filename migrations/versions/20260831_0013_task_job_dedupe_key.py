"""增加任务租户内幂等键。

Revision ID: 20260831_0013
Revises: 20260831_0012
Create Date: 2026-08-31 21:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260831_0013"
down_revision: Union[str, Sequence[str], None] = "20260831_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为需要强幂等的任务增加可空键，不改变现有普通任务行为。"""

    with op.batch_alter_table("task_jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dedupe_key", sa.String(length=191), nullable=True))
        batch_op.create_index("ix_task_jobs_dedupe_key", ["dedupe_key"], unique=False)
        batch_op.create_unique_constraint(
            "uq_task_jobs_tenant_dedupe_key",
            ["tenant_id", "dedupe_key"],
        )


def downgrade() -> None:
    """删除任务幂等约束和字段。"""

    with op.batch_alter_table("task_jobs", schema=None) as batch_op:
        batch_op.drop_constraint("uq_task_jobs_tenant_dedupe_key", type_="unique")
        batch_op.drop_index("ix_task_jobs_dedupe_key")
        batch_op.drop_column("dedupe_key")
