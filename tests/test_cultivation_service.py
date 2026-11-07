from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "cultivation-service"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.cultivation import (
    CultivationCooldown,
    CultivationInventory,
    CultivationPlayer,
    CultivationTransaction,
)
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Message, Plugin, PointsAccount, PointsTransaction
from app.models.saas import Tenant
from app.services.cultivation import CultivationCommandContext, CultivationRules, CultivationService


@pytest.fixture()
def cultivation_fixture(tmp_path: Path):
    """准备一个租户、两个玩家和多条真实消息，供事务与幂等测试复用。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'cultivation.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="cultivation", name="修仙租户", status="active")
        plugin = Plugin(code="xiuxian", display_name="文字修仙", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="修仙机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json="{}",
        )
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="修仙 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add_all([binding, channel])
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="cultivation-group",
            title="修仙群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()

        # 每次写命令都使用真实入站消息主键，测试与生产的流水外键和消息级
        # 幂等键保持一致，避免用虚构编号掩盖提交边界问题。
        messages: dict[str, int] = {}
        for key, sender_id, sender_name, content in (
            ("exchange", "user-a", "甲玩家", "积分购石 100"),
            ("gift", "user-a", "甲玩家", "赠送 user-b 聚灵符 2"),
            ("breakthrough", "user-a", "甲玩家", "突破"),
            ("sign", "user-a", "甲玩家", "修仙签到"),
            ("sign_again", "user-a", "甲玩家", "修仙签到"),
        ):
            message = Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction="in",
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
                message_type="text",
                status="received",
            )
            db.add(message)
            db.flush()
            messages[key] = int(message.id)

        player_a = CultivationPlayer(
            tenant_id=tenant.id,
            bot_plugin_id=binding.id,
            conversation_id=conversation.id,
            member_external_id="user-a",
            display_name="甲玩家",
            dao_name="青云子",
            realm="炼气",
            realm_level=10,
            spirit_root="凡灵根",
            cultivation_points=9_000,
            spirit_stones=0,
            status="idle",
        )
        player_b = CultivationPlayer(
            tenant_id=tenant.id,
            bot_plugin_id=binding.id,
            conversation_id=conversation.id,
            member_external_id="user-b",
            display_name="乙玩家",
            dao_name="玄真子",
            realm="筑基",
            realm_level=1,
            spirit_root="地灵根",
            cultivation_points=0,
            spirit_stones=0,
            status="idle",
        )
        db.add_all([player_a, player_b])
        db.flush()
        db.add_all(
            [
                CultivationInventory(
                    tenant_id=tenant.id,
                    player_id=player_a.id,
                    item_code="聚灵符",
                    item_type="材料",
                    quantity=5,
                ),
                CultivationInventory(
                    tenant_id=tenant.id,
                    player_id=player_a.id,
                    item_code="筑基丹",
                    item_type="丹药",
                    quantity=1,
                ),
                PointsAccount(
                    tenant_id=tenant.id,
                    user_id="user-a",
                    group_id=conversation.external_id,
                    user_name="甲玩家",
                    total_points=1_000,
                ),
            ]
        )
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "binding_id": int(binding.id),
            "conversation_id": int(conversation.id),
            "group_external_id": str(conversation.external_id),
            "player_a_id": int(player_a.id),
            "player_b_id": int(player_b.id),
            "messages": messages,
        }
    try:
        yield factory, identity
    finally:
        engine.dispose()


def _context(identity: dict[str, object], message_key: str) -> CultivationCommandContext:
    """构造已经由插件层校验过的最小命令身份。"""

    messages = identity["messages"]
    assert isinstance(messages, dict)
    return CultivationCommandContext(
        tenant_id=int(identity["tenant_id"]),
        binding_id=int(identity["binding_id"]),
        conversation_id=int(identity["conversation_id"]),
        message_id=int(messages[message_key]),
        member_external_id="user-a",
        display_name="甲玩家",
        group_external_id=str(identity["group_external_id"]),
    )


def test_points_exchange_commits_once_with_cultivation_assets(cultivation_fixture) -> None:
    """积分扣减、灵石入账和两类流水必须一次提交，并可按消息稳定重放。"""

    factory, identity = cultivation_fixture
    rules = CultivationRules(point_to_stone_rate=10)
    context = _context(identity, "exchange")
    with tenant_scope(int(identity["tenant_id"])), tenant_session(factory) as db:
        service = CultivationService(db, context)
        first = service.execute("积分购石", "100", rules)
        repeated = service.execute("积分购石", "100", rules)

    assert first.ok is True
    assert first.code == "points_exchanged"
    assert repeated == first
    with tenant_scope(int(identity["tenant_id"])), tenant_session(factory) as db:
        player = db.get(CultivationPlayer, int(identity["player_a_id"]))
        account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.user_id == "user-a",
                PointsAccount.group_id == identity["group_external_id"],
            )
        )
        cultivation_count = db.scalar(select(func.count()).select_from(CultivationTransaction))
        points_count = db.scalar(select(func.count()).select_from(PointsTransaction))
        assert player is not None and player.spirit_stones == 10
        assert account is not None and account.total_points == 900
        assert cultivation_count == 2
        assert points_count == 1


def test_item_gift_moves_inventory_between_players(cultivation_fixture) -> None:
    """物品赠送必须同时锁定双方玩家并生成一减一增两条资产流水。"""

    factory, identity = cultivation_fixture
    with tenant_scope(int(identity["tenant_id"])), tenant_session(factory) as db:
        result = CultivationService(db, _context(identity, "gift")).execute(
            "赠送",
            "user-b 聚灵符 2",
            CultivationRules(),
        )

    assert result.ok is True
    assert result.code == "item_gifted"
    with tenant_scope(int(identity["tenant_id"])), tenant_session(factory) as db:
        rows = db.scalars(
            select(CultivationInventory)
            .where(CultivationInventory.item_code == "聚灵符")
            .order_by(CultivationInventory.player_id.asc())
        ).all()
        assert [(row.player_id, row.quantity) for row in rows] == [
            (int(identity["player_a_id"]), 3),
            (int(identity["player_b_id"]), 2),
        ]
        transfers = db.scalars(
            select(CultivationTransaction).where(
                CultivationTransaction.transaction_type == "赠送",
                CultivationTransaction.resource_type == "item",
            )
        ).all()
        assert sorted(row.amount for row in transfers) == [-2, 2]


def test_breakthrough_updates_realm_and_consumes_pill(cultivation_fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """普通突破成功后扣除修为和丹药，并把玩家推进到下一境界。"""

    factory, identity = cultivation_fixture
    with tenant_scope(int(identity["tenant_id"])), tenant_session(factory) as db:
        service = CultivationService(db, _context(identity, "breakthrough"))
        # 固定到成功区间，只验证突破事务本身，不让散列结果影响断言稳定性。
        monkeypatch.setattr(service, "_deterministic_unit", lambda _salt: 0.0)
        result = service.execute("突破", "", CultivationRules())

    assert result.ok is True
    assert result.code == "breakthrough_succeeded"
    with tenant_scope(int(identity["tenant_id"])), tenant_session(factory) as db:
        player = db.get(CultivationPlayer, int(identity["player_a_id"]))
        pill = db.scalar(
            select(CultivationInventory).where(
                CultivationInventory.player_id == int(identity["player_a_id"]),
                CultivationInventory.item_code == "筑基丹",
            )
        )
        assert player is not None
        assert (player.realm, player.realm_level, player.cultivation_points) == ("筑基", 1, 0)
        assert pill is not None and pill.quantity == 0


def test_shared_cooldown_blocks_same_action_on_new_message(cultivation_fixture) -> None:
    """首条成功命令写入数据库冷却后，新消息不能绕过多进程共享限制。"""

    factory, identity = cultivation_fixture
    rules = CultivationRules(cooldowns={"修仙签到": 60})
    with tenant_scope(int(identity["tenant_id"])), tenant_session(factory) as db:
        first = CultivationService(db, _context(identity, "sign")).execute("修仙签到", "", rules)
        second = CultivationService(db, _context(identity, "sign_again")).execute("修仙签到", "", rules)
        cooldown_count = db.scalar(select(func.count()).select_from(CultivationCooldown))

    assert first.ok is True
    assert first.code == "signed"
    assert second.ok is False
    assert second.code == "cooldown"
    assert cooldown_count == 1
