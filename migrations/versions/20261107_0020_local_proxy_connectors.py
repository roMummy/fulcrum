"""增加租户 Windows 本地代理连接器及扫码会话临时凭证。

Revision ID: 20261107_0020
Revises: 20260901_0019
Create Date: 2026-11-07 10:30:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261107_0020"
down_revision: Union[str, Sequence[str], None] = "20260901_0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建连接器凭证表，再扩展扫码会话的本地连接器字段。"""

    op.create_table(
        "proxy_connectors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("device_id", sa.String(length=128), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_version", sa.String(length=50), nullable=True),
        sa.Column("local_proxy_label", sa.String(length=255), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'disabled')",
            name="ck_proxy_connectors_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_proxy_connectors_tenant_name"),
        sa.UniqueConstraint("device_id", name="uq_proxy_connectors_device_id"),
    )
    with op.batch_alter_table("proxy_connectors", schema=None) as batch_op:
        batch_op.create_index("ix_proxy_connectors_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index("ix_proxy_connectors_status", ["status"], unique=False)
        batch_op.create_index("ix_proxy_connectors_device_id", ["device_id"], unique=False)
        batch_op.create_index("ix_proxy_connectors_last_seen_at", ["last_seen_at"], unique=False)
        batch_op.create_index(
            "ix_proxy_connectors_tenant_status",
            ["tenant_id", "status", "last_seen_at"],
            unique=False,
        )

    op.create_table(
        "proxy_connector_activation_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connector_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connector_id"], ["proxy_connectors.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["tenant_users.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash", name="uq_proxy_connector_activation_code_hash"),
    )
    with op.batch_alter_table("proxy_connector_activation_codes", schema=None) as batch_op:
        batch_op.create_index("ix_proxy_connector_activation_codes_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index("ix_proxy_connector_activation_codes_connector_id", ["connector_id"], unique=False)
        batch_op.create_index("ix_proxy_connector_activation_codes_code_hash", ["code_hash"], unique=False)
        batch_op.create_index("ix_proxy_connector_activation_codes_expires_at", ["expires_at"], unique=False)
        batch_op.create_index(
            "ix_proxy_connector_activation_codes_connector",
            ["connector_id", "expires_at"],
            unique=False,
        )

    op.create_table(
        "proxy_connector_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("connector_id", sa.Integer(), nullable=False),
        sa.Column("device_key_hash", sa.String(length=64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connector_id"], ["proxy_connectors.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_key_hash", name="uq_proxy_connector_credential_key_hash"),
    )
    with op.batch_alter_table("proxy_connector_credentials", schema=None) as batch_op:
        batch_op.create_index("ix_proxy_connector_credentials_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index("ix_proxy_connector_credentials_connector_id", ["connector_id"], unique=False)
        batch_op.create_index("ix_proxy_connector_credentials_device_key_hash", ["device_key_hash"], unique=False)
        batch_op.create_index("ix_proxy_connector_credentials_revoked_at", ["revoked_at"], unique=False)
        batch_op.create_index(
            "ix_proxy_connector_credentials_connector",
            ["connector_id", "revoked_at"],
            unique=False,
        )

    with op.batch_alter_table("login_proxy_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("proxy_connector_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("relay_username_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("relay_password_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("relay_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column("proxy_cleanup_state", sa.String(length=20), nullable=False, server_default="not_required")
        )
        batch_op.add_column(sa.Column("proxy_cleanup_error", sa.String(length=100), nullable=True))
        batch_op.create_foreign_key(
            "fk_login_proxy_sessions_proxy_connector_id",
            "proxy_connectors",
            ["proxy_connector_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_login_proxy_sessions_proxy_cleanup_state",
            "proxy_cleanup_state IN ('not_required', 'pending', 'completed', 'failed')",
        )
        batch_op.create_index("ix_login_proxy_sessions_proxy_connector_id", ["proxy_connector_id"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_relay_username_hash", ["relay_username_hash"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_relay_expires_at", ["relay_expires_at"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_proxy_cleanup_state", ["proxy_cleanup_state"], unique=False)

    with op.batch_alter_table("login_proxy_sessions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_login_proxy_sessions_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_login_proxy_sessions_mode",
            "mode IN ('direct', 'platform_pool', 'custom', 'local_connector')",
        )


def downgrade() -> None:
    """先移除扫码会话扩展，再按外键依赖删除连接器凭证表。"""

    with op.batch_alter_table("login_proxy_sessions", schema=None) as batch_op:
        batch_op.drop_constraint("ck_login_proxy_sessions_mode", type_="check")
        batch_op.create_check_constraint(
            "ck_login_proxy_sessions_mode",
            "mode IN ('direct', 'platform_pool', 'custom')",
        )
        batch_op.drop_index("ix_login_proxy_sessions_proxy_cleanup_state")
        batch_op.drop_index("ix_login_proxy_sessions_relay_expires_at")
        batch_op.drop_index("ix_login_proxy_sessions_relay_username_hash")
        batch_op.drop_index("ix_login_proxy_sessions_proxy_connector_id")
        batch_op.drop_constraint("ck_login_proxy_sessions_proxy_cleanup_state", type_="check")
        batch_op.drop_constraint("fk_login_proxy_sessions_proxy_connector_id", type_="foreignkey")
        batch_op.drop_column("proxy_cleanup_error")
        batch_op.drop_column("proxy_cleanup_state")
        batch_op.drop_column("relay_expires_at")
        batch_op.drop_column("relay_password_hash")
        batch_op.drop_column("relay_username_hash")
        batch_op.drop_column("proxy_connector_id")

    op.drop_table("proxy_connector_credentials")
    op.drop_table("proxy_connector_activation_codes")
    op.drop_table("proxy_connectors")

