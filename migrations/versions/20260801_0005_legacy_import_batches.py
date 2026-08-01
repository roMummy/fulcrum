"""增加旧系统导入批次审计表。

Revision ID: 20260801_0005
Revises: 20260730_0004
Create Date: 2026-08-01 16:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0005"
down_revision: Union[str, Sequence[str], None] = "20260730_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建带租户和来源唯一约束的导入批次审计表。"""

    op.create_table(
        "legacy_import_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('completed')", name="ck_legacy_import_batches_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_fingerprint", name="uq_legacy_import_tenant_source"),
    )
    with op.batch_alter_table("legacy_import_batches", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_legacy_import_batches_source_fingerprint"), ["source_fingerprint"], unique=False)
        batch_op.create_index(batch_op.f("ix_legacy_import_batches_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_legacy_import_batches_tenant_id"), ["tenant_id"], unique=False)


def downgrade() -> None:
    """删除导入批次审计表，不触碰已经导入的租户业务数据。"""

    with op.batch_alter_table("legacy_import_batches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_legacy_import_batches_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_legacy_import_batches_status"))
        batch_op.drop_index(batch_op.f("ix_legacy_import_batches_source_fingerprint"))
    op.drop_table("legacy_import_batches")
