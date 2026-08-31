"""增加多租户修仙玩法数据表。

Revision ID: 20260901_0019
Revises: 20260901_0018
Create Date: 2026-09-01 04:35:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260901_0019"
down_revision: Union[str, Sequence[str], None] = "20260901_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """创建玩家、门派、背包、资源流水和共享冷却表。"""

    op.create_table(
        "cultivation_clans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_plugin_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("leader_member_external_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bot_plugin_id"], ["bot_plugins.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
            "name",
            name="uq_cultivation_clans_group_name",
        ),
    )
    op.create_index(
        "ix_cultivation_clans_group",
        "cultivation_clans",
        ["tenant_id", "bot_plugin_id", "conversation_id"],
        unique=False,
    )
    for column_name in (
        "tenant_id",
        "bot_plugin_id",
        "conversation_id",
        "leader_member_external_id",
    ):
        op.create_index(
            f"ix_cultivation_clans_{column_name}",
            "cultivation_clans",
            [column_name],
            unique=False,
        )

    op.create_table(
        "cultivation_players",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_plugin_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("member_external_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("dao_name", sa.String(length=64), nullable=False),
        sa.Column("realm", sa.String(length=32), nullable=False),
        sa.Column("realm_level", sa.Integer(), nullable=False),
        sa.Column("spirit_root", sa.String(length=32), nullable=False),
        sa.Column("cultivation_points", sa.BigInteger(), nullable=False),
        sa.Column("spirit_stones", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("status_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cultivation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clan_id", sa.Integer(), nullable=True),
        sa.Column("last_sign_date", sa.Date(), nullable=True),
        sa.Column("last_expedition_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_alchemy_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "realm_level >= 1 AND realm_level <= 10",
            name="ck_cultivation_players_realm_level",
        ),
        sa.CheckConstraint(
            "cultivation_points >= 0 AND spirit_stones >= 0",
            name="ck_cultivation_players_non_negative_assets",
        ),
        sa.CheckConstraint(
            "status IN ('idle', 'cultivating', 'injured', 'unstable')",
            name="ck_cultivation_players_status",
        ),
        sa.ForeignKeyConstraint(["bot_plugin_id"], ["bot_plugins.id"]),
        sa.ForeignKeyConstraint(["clan_id"], ["cultivation_clans.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
            "member_external_id",
            name="uq_cultivation_players_group_member",
        ),
    )
    op.create_index(
        "ix_cultivation_players_leaderboard",
        "cultivation_players",
        ["tenant_id", "bot_plugin_id", "conversation_id", "cultivation_points"],
        unique=False,
    )
    for column_name in (
        "tenant_id",
        "bot_plugin_id",
        "conversation_id",
        "member_external_id",
        "status",
        "status_until",
        "clan_id",
        "last_sign_date",
    ):
        op.create_index(
            f"ix_cultivation_players_{column_name}",
            "cultivation_players",
            [column_name],
            unique=False,
        )

    op.create_table(
        "cultivation_inventory",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="ck_cultivation_inventory_quantity"),
        sa.ForeignKeyConstraint(["player_id"], ["cultivation_players.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "player_id",
            "item_code",
            name="uq_cultivation_inventory_player_item",
        ),
    )
    for column_name in ("tenant_id", "player_id", "item_code"):
        op.create_index(
            f"ix_cultivation_inventory_{column_name}",
            "cultivation_inventory",
            [column_name],
            unique=False,
        )

    op.create_table(
        "cultivation_transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("player_id", sa.Integer(), nullable=False),
        sa.Column("counterparty_player_id", sa.Integer(), nullable=True),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("transaction_type", sa.String(length=40), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_code", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=191), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("balance_after >= 0", name="ck_cultivation_transactions_balance"),
        sa.ForeignKeyConstraint(["counterparty_player_id"], ["cultivation_players.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["player_id"], ["cultivation_players.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_cultivation_transactions_idempotency",
        ),
    )
    op.create_index(
        "ix_cultivation_transactions_player_time",
        "cultivation_transactions",
        ["tenant_id", "player_id", "created_at"],
        unique=False,
    )
    for column_name in (
        "tenant_id",
        "player_id",
        "counterparty_player_id",
        "message_id",
        "transaction_type",
        "resource_type",
        "idempotency_key",
    ):
        op.create_index(
            f"ix_cultivation_transactions_{column_name}",
            "cultivation_transactions",
            [column_name],
            unique=False,
        )

    op.create_table(
        "cultivation_cooldowns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bot_plugin_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("member_external_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bot_plugin_id"], ["bot_plugins.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
            "member_external_id",
            "action",
            name="uq_cultivation_cooldowns_action",
        ),
    )
    op.create_index(
        "ix_cultivation_cooldowns_available",
        "cultivation_cooldowns",
        ["tenant_id", "available_at"],
        unique=False,
    )
    for column_name in (
        "tenant_id",
        "bot_plugin_id",
        "conversation_id",
        "member_external_id",
        "available_at",
    ):
        op.create_index(
            f"ix_cultivation_cooldowns_{column_name}",
            "cultivation_cooldowns",
            [column_name],
            unique=False,
        )


def downgrade() -> None:
    """按依赖顺序删除修仙玩法数据表。"""

    op.drop_table("cultivation_cooldowns")
    op.drop_table("cultivation_transactions")
    op.drop_table("cultivation_inventory")
    op.drop_table("cultivation_players")
    op.drop_table("cultivation_clans")
