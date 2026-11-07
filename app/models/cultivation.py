from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TenantOwnedMixin, TimestampMixin


class CultivationClan(Base, TenantOwnedMixin, TimestampMixin):
    """保存一个插件绑定和群会话内唯一存在的修仙门派。"""

    __tablename__ = "cultivation_clans"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
            "name",
            name="uq_cultivation_clans_group_name",
        ),
        Index(
            "ix_cultivation_clans_group",
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_plugin_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"), nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    leader_member_external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)

    bot_plugin = relationship("BotPlugin")
    conversation = relationship("Conversation")


class CultivationPlayer(Base, TenantOwnedMixin, TimestampMixin):
    """保存一个群成员在当前机器人修仙玩法中的唯一角色状态。"""

    __tablename__ = "cultivation_players"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
            "member_external_id",
            name="uq_cultivation_players_group_member",
        ),
        CheckConstraint(
            "realm_level >= 1 AND realm_level <= 10",
            name="ck_cultivation_players_realm_level",
        ),
        CheckConstraint(
            "cultivation_points >= 0 AND spirit_stones >= 0",
            name="ck_cultivation_players_non_negative_assets",
        ),
        CheckConstraint(
            "status IN ('idle', 'cultivating', 'injured', 'unstable')",
            name="ck_cultivation_players_status",
        ),
        Index(
            "ix_cultivation_players_leaderboard",
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
            "cultivation_points",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_plugin_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"), nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    member_external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    dao_name: Mapped[str] = mapped_column(String(64), nullable=False)
    realm: Mapped[str] = mapped_column(String(32), nullable=False, default="炼气")
    realm_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    spirit_root: Mapped[str] = mapped_column(String(32), nullable=False, default="凡灵根")
    cultivation_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    spirit_stones: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="idle", index=True)
    status_until: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    cultivation_started_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clan_id: Mapped[int | None] = mapped_column(
        ForeignKey("cultivation_clans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    last_sign_date: Mapped[Date | None] = mapped_column(Date, nullable=True, index=True)
    last_expedition_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_alchemy_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    bot_plugin = relationship("BotPlugin")
    conversation = relationship("Conversation")
    clan = relationship("CultivationClan")
    inventory = relationship("CultivationInventory", back_populates="player", cascade="all, delete-orphan")


class CultivationInventory(Base, TenantOwnedMixin, TimestampMixin):
    """保存玩家背包内一种当前产品物品的数量。"""

    __tablename__ = "cultivation_inventory"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "player_id",
            "item_code",
            name="uq_cultivation_inventory_player_item",
        ),
        CheckConstraint("quantity >= 0", name="ck_cultivation_inventory_quantity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("cultivation_players.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    item_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    item_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    player = relationship("CultivationPlayer", back_populates="inventory")


class CultivationTransaction(Base, TenantOwnedMixin, TimestampMixin):
    """记录修仙资产变化，使用消息级业务键保证任务重试不会重复结算。"""

    __tablename__ = "cultivation_transactions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_cultivation_transactions_idempotency",
        ),
        CheckConstraint("balance_after >= 0", name="ck_cultivation_transactions_balance"),
        Index(
            "ix_cultivation_transactions_player_time",
            "tenant_id",
            "player_id",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("cultivation_players.id"), nullable=False, index=True)
    counterparty_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("cultivation_players.id"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True, index=True)
    transaction_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    resource_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False, index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    player = relationship("CultivationPlayer", foreign_keys=[player_id])
    counterparty_player = relationship("CultivationPlayer", foreign_keys=[counterparty_player_id])
    message = relationship("Message")


class CultivationCooldown(Base, TenantOwnedMixin, TimestampMixin):
    """保存玩家命令可再次执行的时间，供多进程 Worker 共享冷却状态。"""

    __tablename__ = "cultivation_cooldowns"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "bot_plugin_id",
            "conversation_id",
            "member_external_id",
            "action",
            name="uq_cultivation_cooldowns_action",
        ),
        Index(
            "ix_cultivation_cooldowns_available",
            "tenant_id",
            "available_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_plugin_id: Mapped[int] = mapped_column(ForeignKey("bot_plugins.id"), nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), nullable=False, index=True)
    member_external_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    available_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    bot_plugin = relationship("BotPlugin")
    conversation = relationship("Conversation")
