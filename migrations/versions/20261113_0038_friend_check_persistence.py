"""持久保存微桥好友检测进度和逐项结果。"""

from alembic import op
import sqlalchemy as sa


revision = "20261113_0038"
down_revision = "20261112_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webridge_friend_check_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("generation", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("interval_seconds", sa.Float(), nullable=False),
        sa.Column("elapsed_ms", sa.BigInteger(), nullable=False),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("initialized", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "channel_id", name="uq_webridge_friend_check_runs_tenant_channel"),
    )
    op.create_index("ix_webridge_friend_check_runs_channel_id", "webridge_friend_check_runs", ["channel_id"])
    op.create_index("ix_webridge_friend_check_runs_tenant_id", "webridge_friend_check_runs", ["tenant_id"])
    op.create_table(
        "webridge_friend_check_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("contact_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["webridge_friend_check_runs.id"]),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "contact_id", name="uq_webridge_friend_check_items_run_contact"),
        sa.UniqueConstraint("run_id", "position", name="uq_webridge_friend_check_items_run_position"),
    )
    op.create_index("ix_webridge_friend_check_items_run_id", "webridge_friend_check_items", ["run_id"])
    op.create_index("ix_webridge_friend_check_items_channel_id", "webridge_friend_check_items", ["channel_id"])
    op.create_index("ix_webridge_friend_check_items_job_id", "webridge_friend_check_items", ["job_id"])
    op.create_index("ix_webridge_friend_check_items_tenant_id", "webridge_friend_check_items", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_webridge_friend_check_items_tenant_id", table_name="webridge_friend_check_items")
    op.drop_index("ix_webridge_friend_check_items_job_id", table_name="webridge_friend_check_items")
    op.drop_index("ix_webridge_friend_check_items_channel_id", table_name="webridge_friend_check_items")
    op.drop_index("ix_webridge_friend_check_items_run_id", table_name="webridge_friend_check_items")
    op.drop_table("webridge_friend_check_items")
    op.drop_index("ix_webridge_friend_check_runs_tenant_id", table_name="webridge_friend_check_runs")
    op.drop_index("ix_webridge_friend_check_runs_channel_id", table_name="webridge_friend_check_runs")
    op.drop_table("webridge_friend_check_runs")
