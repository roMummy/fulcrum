"""增加开放接口套餐额度和月度使用账本。

Revision ID: 20261108_0021
Revises: 20261107_0020
Create Date: 2026-11-08 14:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261108_0021"
down_revision: Union[str, Sequence[str], None] = "20261107_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """为已有套餐补充开放能力额度，并创建可幂等结算的使用流水表。"""

    with op.batch_alter_table("saas_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("open_api_app_limit", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("open_api_channel_limit", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("monthly_api_request_limit", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("monthly_message_limit", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("monthly_media_bytes_limit", sa.Integer(), nullable=False, server_default="0"))
        batch_op.create_check_constraint(
            "ck_saas_plans_open_api_app_limit",
            "open_api_app_limit >= 0",
        )
        batch_op.create_check_constraint(
            "ck_saas_plans_open_api_channel_limit",
            "open_api_channel_limit >= 0",
        )
        batch_op.create_check_constraint(
            "ck_saas_plans_monthly_api_request_limit",
            "monthly_api_request_limit >= 0",
        )
        batch_op.create_check_constraint(
            "ck_saas_plans_monthly_message_limit",
            "monthly_message_limit >= 0",
        )
        batch_op.create_check_constraint(
            "ck_saas_plans_monthly_media_bytes_limit",
            "monthly_media_bytes_limit >= 0",
        )

    op.create_table(
        "open_api_usage_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("app_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("metric", sa.String(length=40), nullable=False),
        sa.Column("request_id", sa.String(length=191), nullable=False),
        sa.Column("reservation_key", sa.String(length=255), nullable=False),
        sa.Column("units", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="reserved"),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('reserved', 'committed', 'refunded')",
            name="ck_open_api_usage_records_status",
        ),
        sa.CheckConstraint("units > 0", name="ck_open_api_usage_records_units"),
        sa.ForeignKeyConstraint(["app_id"], ["open_api_apps.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reservation_key", name="uq_open_api_usage_records_reservation_key"),
    )
    with op.batch_alter_table("open_api_usage_records", schema=None) as batch_op:
        batch_op.create_index(
            "ix_open_api_usage_records_app_id",
            ["app_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_open_api_usage_records_period",
            ["period"],
            unique=False,
        )
        batch_op.create_index(
            "ix_open_api_usage_records_metric",
            ["metric"],
            unique=False,
        )
        batch_op.create_index(
            "ix_open_api_usage_records_status",
            ["status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_open_api_usage_records_period_metric",
            ["tenant_id", "period", "metric", "status"],
            unique=False,
        )
        batch_op.create_index(
            "ix_open_api_usage_records_app_period",
            ["app_id", "period", "metric"],
            unique=False,
        )


def downgrade() -> None:
    """按逆序删除使用流水和套餐额度字段。"""

    op.drop_table("open_api_usage_records")
    with op.batch_alter_table("saas_plans", schema=None) as batch_op:
        batch_op.drop_constraint("ck_saas_plans_monthly_media_bytes_limit", type_="check")
        batch_op.drop_constraint("ck_saas_plans_monthly_message_limit", type_="check")
        batch_op.drop_constraint("ck_saas_plans_monthly_api_request_limit", type_="check")
        batch_op.drop_constraint("ck_saas_plans_open_api_channel_limit", type_="check")
        batch_op.drop_constraint("ck_saas_plans_open_api_app_limit", type_="check")
        batch_op.drop_column("monthly_media_bytes_limit")
        batch_op.drop_column("monthly_message_limit")
        batch_op.drop_column("monthly_api_request_limit")
        batch_op.drop_column("open_api_channel_limit")
        batch_op.drop_column("open_api_app_limit")
