"""provider nodes and device credentials

Revision ID: 20260730_0004
Revises: 20260730_0003
Create Date: 2026-08-01 13:50:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0004"
down_revision: str | Sequence[str] | None = "20260730_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立节点身份链，并为渠道和任务增加不可由客户端改选的绑定字段。"""

    op.create_table(
        "provider_nodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("device_id", sa.String(length=100), nullable=True),
        sa.Column("client_version", sa.String(length=100), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'active', 'disabled')", name="ck_provider_nodes_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_provider_nodes_tenant_name"),
    )
    op.create_index(op.f("ix_provider_nodes_status"), "provider_nodes", ["status"], unique=False)
    op.create_index(op.f("ix_provider_nodes_tenant_id"), "provider_nodes", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_provider_nodes_last_heartbeat_at"), "provider_nodes", ["last_heartbeat_at"], unique=False)
    op.create_index(op.f("ix_provider_nodes_device_id"), "provider_nodes", ["device_id"], unique=True)

    op.create_table(
        "provider_node_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_node_id", sa.Integer(), nullable=False),
        sa.Column("account_key", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=150), nullable=True),
        sa.Column("external_user_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('online', 'offline', 'unknown')", name="ck_provider_node_accounts_status"),
        sa.ForeignKeyConstraint(["provider_node_id"], ["provider_nodes.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_node_id", "account_key", name="uq_provider_node_account_key"),
    )
    op.create_index(op.f("ix_provider_node_accounts_provider_node_id"), "provider_node_accounts", ["provider_node_id"], unique=False)
    op.create_index(op.f("ix_provider_node_accounts_tenant_id"), "provider_node_accounts", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_provider_node_accounts_external_user_id"), "provider_node_accounts", ["external_user_id"], unique=False)
    op.create_index(op.f("ix_provider_node_accounts_status"), "provider_node_accounts", ["status"], unique=False)
    op.create_index(op.f("ix_provider_node_accounts_last_seen_at"), "provider_node_accounts", ["last_seen_at"], unique=False)

    op.create_table(
        "provider_activation_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_node_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["tenant_users.id"]),
        sa.ForeignKeyConstraint(["provider_node_id"], ["provider_nodes.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_activation_codes_provider_node_id"), "provider_activation_codes", ["provider_node_id"], unique=False)
    op.create_index(op.f("ix_provider_activation_codes_tenant_id"), "provider_activation_codes", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_provider_activation_codes_code_hash"), "provider_activation_codes", ["code_hash"], unique=True)
    op.create_index(op.f("ix_provider_activation_codes_expires_at"), "provider_activation_codes", ["expires_at"], unique=False)
    op.create_index(op.f("ix_provider_activation_codes_consumed_at"), "provider_activation_codes", ["consumed_at"], unique=False)
    op.create_index(op.f("ix_provider_activation_codes_created_by_user_id"), "provider_activation_codes", ["created_by_user_id"], unique=False)

    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_node_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=100), nullable=False),
        sa.Column("fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("client_version", sa.String(length=100), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=255), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_node_id"], ["provider_nodes.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_credentials_provider_node_id"), "provider_credentials", ["provider_node_id"], unique=False)
    op.create_index(op.f("ix_provider_credentials_tenant_id"), "provider_credentials", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_provider_credentials_token_hash"), "provider_credentials", ["token_hash"], unique=True)
    op.create_index(op.f("ix_provider_credentials_device_id"), "provider_credentials", ["device_id"], unique=False)
    op.create_index(op.f("ix_provider_credentials_last_used_at"), "provider_credentials", ["last_used_at"], unique=False)
    op.create_index(op.f("ix_provider_credentials_revoked_at"), "provider_credentials", ["revoked_at"], unique=False)

    op.create_table(
        "provider_heartbeats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_node_id", sa.Integer(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("client_version", sa.String(length=100), nullable=True),
        sa.Column("hook_status", sa.String(length=50), nullable=True),
        sa.Column("workwechat_status", sa.String(length=50), nullable=True),
        sa.Column("accounts_json", sa.Text(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["provider_node_id"], ["provider_nodes.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_provider_heartbeats_provider_node_id"), "provider_heartbeats", ["provider_node_id"], unique=False)
    op.create_index(op.f("ix_provider_heartbeats_tenant_id"), "provider_heartbeats", ["tenant_id"], unique=False)
    op.create_index(op.f("ix_provider_heartbeats_reported_at"), "provider_heartbeats", ["reported_at"], unique=False)

    with op.batch_alter_table("channels") as batch_op:
        batch_op.add_column(sa.Column("provider_node_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("provider_account_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_channels_provider_node_id"), ["provider_node_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_channels_provider_account_id"), ["provider_account_id"], unique=False)
        batch_op.create_foreign_key("fk_channels_provider_node_id", "provider_nodes", ["provider_node_id"], ["id"])
        batch_op.create_foreign_key("fk_channels_provider_account_id", "provider_node_accounts", ["provider_account_id"], ["id"])

    with op.batch_alter_table("task_jobs") as batch_op:
        batch_op.add_column(sa.Column("provider_node_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("provider_account_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("lease_token_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(batch_op.f("ix_task_jobs_provider_node_id"), ["provider_node_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_task_jobs_provider_account_id"), ["provider_account_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_task_jobs_lease_token_hash"), ["lease_token_hash"], unique=False)
        batch_op.create_index(batch_op.f("ix_task_jobs_lease_expires_at"), ["lease_expires_at"], unique=False)
        batch_op.create_foreign_key("fk_task_jobs_provider_node_id", "provider_nodes", ["provider_node_id"], ["id"])
        batch_op.create_foreign_key("fk_task_jobs_provider_account_id", "provider_node_accounts", ["provider_account_id"], ["id"])


def downgrade() -> None:
    """先解除业务表引用，再按依赖顺序删除 Provider 身份表。"""

    with op.batch_alter_table("task_jobs") as batch_op:
        batch_op.drop_constraint("fk_task_jobs_provider_account_id", type_="foreignkey")
        batch_op.drop_constraint("fk_task_jobs_provider_node_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_task_jobs_lease_expires_at"))
        batch_op.drop_index(batch_op.f("ix_task_jobs_lease_token_hash"))
        batch_op.drop_index(batch_op.f("ix_task_jobs_provider_account_id"))
        batch_op.drop_index(batch_op.f("ix_task_jobs_provider_node_id"))
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_token_hash")
        batch_op.drop_column("provider_account_id")
        batch_op.drop_column("provider_node_id")

    with op.batch_alter_table("channels") as batch_op:
        batch_op.drop_constraint("fk_channels_provider_account_id", type_="foreignkey")
        batch_op.drop_constraint("fk_channels_provider_node_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_channels_provider_account_id"))
        batch_op.drop_index(batch_op.f("ix_channels_provider_node_id"))
        batch_op.drop_column("provider_account_id")
        batch_op.drop_column("provider_node_id")

    op.drop_table("provider_heartbeats")
    op.drop_table("provider_credentials")
    op.drop_table("provider_activation_codes")
    op.drop_table("provider_node_accounts")
    op.drop_table("provider_nodes")
