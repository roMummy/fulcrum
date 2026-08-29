"""增加平台代理池和租户扫码登录代理会话。

Revision ID: 20260829_0010
Revises: 20260828_0009
Create Date: 2026-08-29 21:20:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260829_0010"
down_revision: Union[str, Sequence[str], None] = "20260828_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """先创建平台代理资源，再创建带租户归属的临时登录会话。"""

    op.create_table(
        "platform_proxies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("province", sa.String(length=64), nullable=False),
        sa.Column("city", sa.String(length=64), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=191), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("health_status", sa.String(length=20), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("lease_key", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result_summary", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name="ck_platform_proxies_port"),
        sa.CheckConstraint("consecutive_failures >= 0", name="ck_platform_proxies_failures"),
        sa.CheckConstraint(
            "health_status IN ('healthy', 'unhealthy')",
            name="ck_platform_proxies_health_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host", "port", "username", name="uq_platform_proxy_endpoint"),
    )
    with op.batch_alter_table("platform_proxies", schema=None) as batch_op:
        batch_op.create_index("ix_platform_proxies_province", ["province"], unique=False)
        batch_op.create_index("ix_platform_proxies_city", ["city"], unique=False)
        batch_op.create_index("ix_platform_proxies_is_enabled", ["is_enabled"], unique=False)
        batch_op.create_index("ix_platform_proxies_health_status", ["health_status"], unique=False)
        batch_op.create_index("ix_platform_proxies_lease_key", ["lease_key"], unique=True)
        batch_op.create_index("ix_platform_proxies_lease_expires_at", ["lease_expires_at"], unique=False)
        batch_op.create_index(
            "ix_platform_proxies_allocation",
            ["province", "city", "is_enabled", "health_status", "lease_expires_at"],
            unique=False,
        )

    op.create_table(
        "login_proxy_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("session_key", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("province", sa.String(length=64), nullable=True),
        sa.Column("city", sa.String(length=64), nullable=True),
        sa.Column("platform_proxy_id", sa.Integer(), nullable=True),
        sa.Column("custom_host", sa.String(length=255), nullable=True),
        sa.Column("custom_port", sa.Integer(), nullable=True),
        sa.Column("custom_username", sa.String(length=191), nullable=True),
        sa.Column("custom_password_encrypted", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("attempted_proxy_ids_json", sa.Text(), nullable=True),
        sa.Column("qrcode_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("result_summary", sa.String(length=255), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_count >= 0", name="ck_login_proxy_sessions_attempt_count"),
        sa.CheckConstraint(
            "custom_port IS NULL OR (custom_port >= 1 AND custom_port <= 65535)",
            name="ck_login_proxy_sessions_custom_port",
        ),
        sa.CheckConstraint(
            "mode IN ('direct', 'platform_pool', 'custom')",
            name="ck_login_proxy_sessions_mode",
        ),
        sa.CheckConstraint(
            "state IN ('preparing', 'qrcode_ready', 'completed', 'failed', 'cancelled', 'expired')",
            name="ck_login_proxy_sessions_state",
        ),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.ForeignKeyConstraint(["platform_proxy_id"], ["platform_proxies.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("login_proxy_sessions", schema=None) as batch_op:
        batch_op.create_index("ix_login_proxy_sessions_channel_id", ["channel_id"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_session_key", ["session_key"], unique=True)
        batch_op.create_index("ix_login_proxy_sessions_mode", ["mode"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_platform_proxy_id", ["platform_proxy_id"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_state", ["state"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_expires_at", ["expires_at"], unique=False)
        batch_op.create_index("ix_login_proxy_sessions_tenant_id", ["tenant_id"], unique=False)
        batch_op.create_index(
            "ix_login_proxy_sessions_channel_state",
            ["tenant_id", "channel_id", "state", "expires_at"],
            unique=False,
        )


def downgrade() -> None:
    """先删除租户登录会话，再删除其引用的平台代理资源。"""

    with op.batch_alter_table("login_proxy_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_login_proxy_sessions_channel_state")
        batch_op.drop_index("ix_login_proxy_sessions_tenant_id")
        batch_op.drop_index("ix_login_proxy_sessions_expires_at")
        batch_op.drop_index("ix_login_proxy_sessions_state")
        batch_op.drop_index("ix_login_proxy_sessions_platform_proxy_id")
        batch_op.drop_index("ix_login_proxy_sessions_mode")
        batch_op.drop_index("ix_login_proxy_sessions_session_key")
        batch_op.drop_index("ix_login_proxy_sessions_channel_id")
    op.drop_table("login_proxy_sessions")

    with op.batch_alter_table("platform_proxies", schema=None) as batch_op:
        batch_op.drop_index("ix_platform_proxies_allocation")
        batch_op.drop_index("ix_platform_proxies_lease_expires_at")
        batch_op.drop_index("ix_platform_proxies_lease_key")
        batch_op.drop_index("ix_platform_proxies_health_status")
        batch_op.drop_index("ix_platform_proxies_is_enabled")
        batch_op.drop_index("ix_platform_proxies_city")
        batch_op.drop_index("ix_platform_proxies_province")
    op.drop_table("platform_proxies")
