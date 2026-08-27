"""增加平台 SaaS 数据大盘组合索引。

Revision ID: 20260827_0008
Revises: 20260821_0007
Create Date: 2026-08-27 10:30:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260827_0008"
down_revision: Union[str, Sequence[str], None] = "20260821_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建平台分析所需索引，不修改业务字段、状态或历史数据。"""

    op.create_index(
        "ix_messages_platform_analytics",
        "messages",
        ["created_at", "direction", "tenant_id", "sender_id"],
        unique=False,
    )
    op.create_index(
        "ix_message_activity_hourly_platform_analytics",
        "message_activity_hourly",
        ["bucket_start", "tenant_id", "direction"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_sessions_platform_presence",
        "tenant_sessions",
        ["last_seen_at", "revoked_at", "expires_at", "tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_channels_platform_health",
        "channels",
        ["tenant_id", "is_enabled", "status", "login_state"],
        unique=False,
    )
    op.create_index(
        "ix_billing_orders_platform_revenue",
        "billing_orders",
        ["status", "reviewed_at"],
        unique=False,
    )
    op.create_index(
        "ix_tenant_subscriptions_platform_health",
        "tenant_subscriptions",
        ["status", "expires_at", "tenant_id", "plan_id"],
        unique=False,
    )
    op.create_index(
        "ix_bot_bindings_platform_usage",
        "bot_bindings",
        ["status", "tenant_id", "adapter_type"],
        unique=False,
    )
    op.create_index(
        "ix_provider_nodes_platform_presence",
        "provider_nodes",
        ["status", "last_heartbeat_at", "tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """按创建逆序删除平台分析索引，保留全部业务数据。"""

    op.drop_index("ix_provider_nodes_platform_presence", table_name="provider_nodes")
    op.drop_index("ix_bot_bindings_platform_usage", table_name="bot_bindings")
    op.drop_index("ix_tenant_subscriptions_platform_health", table_name="tenant_subscriptions")
    op.drop_index("ix_billing_orders_platform_revenue", table_name="billing_orders")
    op.drop_index("ix_channels_platform_health", table_name="channels")
    op.drop_index("ix_tenant_sessions_platform_presence", table_name="tenant_sessions")
    op.drop_index(
        "ix_message_activity_hourly_platform_analytics",
        table_name="message_activity_hourly",
    )
    op.drop_index("ix_messages_platform_analytics", table_name="messages")
