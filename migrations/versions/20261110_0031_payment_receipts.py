"""新增 payment_receipts:自动抢红包、自动收款的收款记录。

Revision ID: 20261110_0031
Revises: 20261110_0030
Create Date: 2026-11-10 23:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261110_0031"
down_revision: Union[str, Sequence[str], None] = "20261110_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """只新增表,不改动已有数据;同一渠道里同一种、同一个 ID 只有一行。"""

    op.create_table(
        "payment_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=191), nullable=False),
        sa.Column("chat_type", sa.String(length=20), nullable=False),
        sa.Column("chat_external_id", sa.String(length=191), nullable=False),
        sa.Column("chat_title", sa.String(length=200), nullable=True),
        sa.Column("sender_id", sa.String(length=191), nullable=True),
        sa.Column("sender_name", sa.String(length=200), nullable=True),
        sa.Column("amount_fen", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result_status", sa.String(length=60), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel_id", "kind", "external_id", name="uq_payment_receipts_target"),
    )
    with op.batch_alter_table("payment_receipts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_payment_receipts_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_payment_receipts_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_payment_receipts_kind"), ["kind"], unique=False)
        batch_op.create_index(batch_op.f("ix_payment_receipts_chat_external_id"), ["chat_external_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_payment_receipts_status"), ["status"], unique=False)
        # 明细按时间倒序展示、统计按时间段汇总
        batch_op.create_index("ix_payment_receipts_channel_created", ["channel_id", "created_at"], unique=False)


def downgrade() -> None:
    """删除收款记录表(记录只用于查询展示,不影响红包/转账本身)。"""

    op.drop_table("payment_receipts")
