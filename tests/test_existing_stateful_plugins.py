from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "existing-stateful-plugins"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import now_shanghai
from app.models.entities import (
    Bot,
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    Message,
    Plugin,
    PointsAccount,
    PointsPrisonRecord,
    PointsTransaction,
    PluginStateRecord,
    SignInHistory,
    SignInRecord,
    TaskJob,
)
from app.models.saas import Tenant
from app.services.points import BAILOUT_COST, PointsService
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse
from app.services.plugin_runtime.state_store import PluginStateStore


@pytest.fixture()
def game_task_fixture(tmp_path: Path):
    """建立一个租户、两个群会话和游戏绑定，供可信状态与积分链路验证。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'game-task.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="game-task", name="游戏任务租户", status="active")
        plugin = Plugin(code="game_task", display_name="游戏任务", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="游戏机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="游戏 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="game-room",
            title="游戏群",
            chat_type="group",
        )
        forged_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="forged-room",
            title="伪造群",
            chat_type="group",
        )
        db.add_all([conversation, forged_conversation])
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "forged_conversation_id": int(forged_conversation.id),
            "binding_id": int(binding.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def jd_token_fixture(tmp_path: Path):
    """建立京东令牌私聊会话和独立插件绑定，验证租户青龙写入边界。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'jd-token.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="jd-token", name="京东令牌租户", status="active")
        plugin = Plugin(code="jd_sign_token", display_name="京东签到令牌", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="京东机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="京东 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="jd-user-chat",
            title="京东用户",
            chat_type="direct",
        )
        db.add(conversation)
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "binding_id": int(binding.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def lucky_pot_fixture(tmp_path: Path):
    """建立幸运池群会话和插件绑定，验证积分守恒、调度与退款。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'lucky-pot.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="lucky-pot", name="幸运池租户", status="active")
        plugin = Plugin(code="lucky_pot", display_name="幸运池", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="幸运池机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="幸运池 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="lucky-room",
            title="幸运池群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "binding_id": int(binding.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def message_push_fixture(tmp_path: Path):
    """建立两个群会话和一个定时推送绑定，验证任务始终锁定当前群。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'message-push.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="message-push", name="定时推送租户", status="active")
        plugin = Plugin(code="message_push_task", display_name="定时推送", plugin_type="scheduled")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="推送机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="推送 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="push-room",
            title="推送群",
            chat_type="group",
        )
        other_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="other-room",
            title="其他群",
            chat_type="group",
        )
        db.add_all([conversation, other_conversation])
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "other_conversation_id": int(other_conversation.id),
            "binding_id": int(binding.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def message_sign_fixture(tmp_path: Path):
    """建立两个群会话和签到绑定，验证用户与群身份只能来自真实消息。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'message-sign.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="message-sign", name="群签到租户", status="active")
        plugin = Plugin(code="message_sign", display_name="群签到", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="签到机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="签到 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="sign-room",
            title="签到群",
            chat_type="group",
        )
        other_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="other-sign-room",
            title="其他签到群",
            chat_type="group",
        )
        db.add_all([conversation, other_conversation])
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "other_conversation_id": int(other_conversation.id),
            "binding_id": int(binding.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def point_trade_fixture(tmp_path: Path):
    """建立积分交易群和标准群成员，验证跨渠道统一目标解析与原子结算。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'point-trade.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="point-trade", name="积分交易租户", status="active")
        plugin = Plugin(code="point_trade", display_name="积分交易", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="积分机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="积分 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="trade-room",
            title="积分群",
            chat_type="group",
        )
        other_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="other-trade-room",
            title="其他积分群",
            chat_type="group",
        )
        db.add_all([conversation, other_conversation])
        db.flush()
        chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            external_id="trade-room",
            name="积分群",
            status="active",
            is_active=True,
        )
        db.add(chatroom)
        db.flush()
        for user_id, name in (
            ("trade-alpha", "甲"),
            ("trade-beta", "乙"),
            ("trade-gamma", "丙"),
            ("trade-delta", "丁"),
        ):
            db.add(
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id=user_id,
                    nickname=name,
                    display_name=name,
                    status="active",
                )
            )
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "other_conversation_id": int(other_conversation.id),
            "binding_id": int(binding.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _game_message_request(
    db,
    identity: dict[str, int],
    *,
    content: str,
    sender_id: str,
    sender_name: str,
    payload: dict[str, object] | None = None,
) -> PluginExecutionRequest:
    """创建已落库入站消息，并用其主键构造平台可信执行上下文。"""

    message = Message(
        tenant_id=identity["tenant_id"],
        conversation_id=identity["conversation_id"],
        direction="in",
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        message_type="text",
        status="received",
    )
    db.add(message)
    db.flush()
    return PluginExecutionRequest(
        action="execute",
        config={"score_multiplier": 2, "ranking_limit": 10, "max_active_tasks": 10},
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=int(message.id),
            sender_id=sender_id,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def _jd_message_request(
    db,
    identity: dict[str, int],
    *,
    content: str,
    payload: dict[str, object] | None = None,
) -> PluginExecutionRequest:
    """把京东命令先落库，再构造只指向该私聊消息的可信上下文。"""

    message = Message(
        tenant_id=identity["tenant_id"],
        conversation_id=identity["conversation_id"],
        direction="in",
        sender_id="jd-user",
        sender_name="京东用户",
        content=content,
        message_type="text",
        status="received",
    )
    db.add(message)
    db.flush()
    return PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "ql_host": "https://ql.example.test",
            "client_id": "tenant-client-id",
            "client_secret": "tenant-client-secret",
            "env_name": "JD_COOKIE",
            "timeout_seconds": 15,
        },
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=int(message.id),
            sender_id="jd-user",
            channel_type="telegram",
            chat_type="direct",
            message_type="text",
        ),
    )


def _lucky_message_request(
    db,
    identity: dict[str, int],
    *,
    content: str,
    sender_id: str,
    sender_name: str,
    max_players: int = 2,
    payload: dict[str, object] | None = None,
) -> PluginExecutionRequest:
    """创建幸运池真实群消息，奖池规则统一来自当前绑定配置。"""

    message = Message(
        tenant_id=identity["tenant_id"],
        conversation_id=identity["conversation_id"],
        direction="in",
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        message_type="text",
        status="received",
    )
    db.add(message)
    db.flush()
    return PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "entry_fee": 10,
            "min_players": 2,
            "max_players": max_players,
            "countdown_seconds": 60,
            "house_cut_percent": 10,
            "lucky_bonus_chance": 0,
            "lucky_bonus_percent": 20,
        },
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=int(message.id),
            sender_id=sender_id,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def _message_push_request(
    db,
    identity: dict[str, int],
    *,
    content: str,
    payload: dict[str, object] | None = None,
) -> PluginExecutionRequest:
    """创建真实群文本消息，测试载荷中的伪造定位字段不会被插件读取。"""

    message = Message(
        tenant_id=identity["tenant_id"],
        conversation_id=identity["conversation_id"],
        direction="in",
        sender_id="push-admin",
        sender_name="群管理员",
        content=content,
        message_type="text",
        status="received",
    )
    db.add(message)
    db.flush()
    return PluginExecutionRequest(
        action="execute",
        config={},
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=int(message.id),
            sender_id="push-admin",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def _message_sign_request(
    db,
    identity: dict[str, int],
    *,
    content: str,
    sender_id: str,
    sender_name: str,
    payload: dict[str, object] | None = None,
) -> PluginExecutionRequest:
    """创建真实签到群消息，载荷仅用于验证伪造字段不会影响业务身份。"""

    message = Message(
        tenant_id=identity["tenant_id"],
        conversation_id=identity["conversation_id"],
        direction="in",
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        message_type="text",
        status="received",
    )
    db.add(message)
    db.flush()
    return PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "min_point": 3,
            "max_point": 50,
            "streak_cycle": 1,
            "max_streak_point": 50,
            "makeup_cost": 10,
        },
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=int(message.id),
            sender_id=sender_id,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def _point_trade_request(
    db,
    identity: dict[str, int],
    *,
    content: str,
    sender_id: str,
    sender_name: str,
    config: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
) -> PluginExecutionRequest:
    """创建真实积分群消息，并允许用 payload 伪造字段验证身份隔离。"""

    message = Message(
        tenant_id=identity["tenant_id"],
        conversation_id=identity["conversation_id"],
        direction="in",
        sender_id=sender_id,
        sender_name=sender_name,
        content=content,
        message_type="text",
        status="received",
    )
    db.add(message)
    db.flush()
    effective_config: dict[str, object] = {
        "enabled": True,
        "ranking_limit": 10,
        "rob_success_rate": 0.4,
        "rob_min_percent": 0.2,
        "rob_max_percent": 0.4,
        "rob_penalty_percent": 0.1,
        "rob_cooldown": 300,
        "rob_min_points": 30,
    }
    effective_config.update(config or {})
    return PluginExecutionRequest(
        action="execute",
        config=effective_config,
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=int(message.id),
            sender_id=sender_id,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def test_points_service_idempotency_prevents_duplicate_balance_changes(tmp_path: Path) -> None:
    """同一租户业务键重复提交时只产生一条积分流水。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'points-idempotency.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with tenant_scope(1), tenant_session(factory) as db:
            service = PointsService(db)
            first = service.add_points(
                "user-1",
                "group-1",
                10,
                source="game_task",
                idempotency_key="game-task:conversation-1:task-1:reward",
            )
            repeated = service.add_points(
                "user-1",
                "group-1",
                10,
                source="game_task",
                idempotency_key="game-task:conversation-1:task-1:reward",
            )
            account = db.scalar(select(PointsAccount).where(PointsAccount.user_id == "user-1"))
            transactions = db.scalars(select(PointsTransaction)).all()

        assert first.ok is True
        assert repeated.ok is True
        assert repeated.extra and repeated.extra["idempotent"] is True
        assert account is not None and account.total_points == 10
        assert len(transactions) == 1
        assert transactions[0].idempotency_key == "game-task:conversation-1:task-1:reward"
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_game_task_uses_trusted_group_state_and_idempotent_points(game_task_fixture, monkeypatch) -> None:
    """游戏命令只读取真实消息，任务答案留在状态表，积分奖励和错答扣分保持幂等。"""

    factory, identity = game_task_fixture
    discovered = get_discovered_plugin("game_task", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("s", "t", "a", "r", "l", "h")
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config["random_task_enabled"] is False

    loaded = load_directory_plugin("game_task", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module.random, "choice", lambda items: items[0])

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        join_alpha = module.execute_message_action(
            db,
            _game_message_request(
                db,
                identity,
                content="/s",
                sender_id="player-alpha",
                sender_name="甲玩家",
                payload={
                    "command_name": "r",
                    "sender_id": "forged-user",
                    "sender_name": "伪造昵称",
                    "conversation_id": identity["forged_conversation_id"],
                },
            ),
        )
        join_beta = module.execute_message_action(
            db,
            _game_message_request(
                db,
                identity,
                content="#s",
                sender_id="player-beta",
                sender_name="乙玩家",
            ),
        )
        # 错答扣分需要已有余额；这笔种子积分使用其他来源，排行榜仍读取统一积分账本。
        PointsService(db).add_points(
            "player-beta",
            "game-room",
            2,
            user_name="乙玩家",
            source="test_seed",
            idempotency_key="test-seed:game-task:player-beta",
        )
        created = module.execute_message_action(
            db,
            _game_message_request(
                db,
                identity,
                content="/t",
                sender_id="player-alpha",
                sender_name="甲玩家",
            ),
        )

        store = PluginStateStore(db, PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            channel_type="telegram",
            chat_type="group",
        ))
        state_key = f"conversation:{identity['conversation_id']}"
        created_state = store.get("game_task", state_key)
        assert created_state is not None
        assert created_state["players"] == {"player-alpha": "甲玩家", "player-beta": "乙玩家"}
        assert created_state["active_tasks"]["1"]["answer"] == "北京"

        wrong_request = _game_message_request(
            db,
            identity,
            content="/a 1 上海",
            sender_id="player-beta",
            sender_name="乙玩家",
            payload={"command_name": "a", "command_args": "999 北京"},
        )
        wrong = module.execute_message_action(db, wrong_request)
        repeated_wrong = module.execute_message_action(db, wrong_request)

        correct_request = _game_message_request(
            db,
            identity,
            content="/a 1 北京",
            sender_id="player-alpha",
            sender_name="甲玩家",
        )
        correct = module.execute_message_action(db, correct_request)
        repeated_correct = module.execute_message_action(db, correct_request)
        ranking = module.execute_message_action(
            db,
            _game_message_request(
                db,
                identity,
                content="/r",
                sender_id="player-alpha",
                sender_name="甲玩家",
            ),
        )

        final_state = store.get("game_task", state_key)
        forged_state = store.get("game_task", f"conversation:{identity['forged_conversation_id']}")
        game_transactions = db.scalars(
            select(PointsTransaction)
            .where(PointsTransaction.source == "game_task")
            .order_by(PointsTransaction.id.asc())
        ).all()
        alpha_account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.user_id == "player-alpha",
                PointsAccount.group_id == "game-room",
            )
        )
        beta_account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.user_id == "player-beta",
                PointsAccount.group_id == "game-room",
            )
        )

    assert join_alpha.output["provider_status"] == "joined"
    assert join_beta.output["provider_status"] == "joined"
    assert created.output["provider_status"] == "task_created"
    assert created.output["task_id"] == 1
    assert "answer" not in created.output
    assert "北京" not in created.output["replies"][0]["content"]
    assert wrong.output["provider_status"] == "answered_incorrect"
    assert wrong.output["total_points"] == 1
    assert repeated_wrong.output["provider_status"] == "answered_incorrect_repeated"
    assert repeated_wrong.output["total_points"] == 1
    assert correct.output["provider_status"] == "answered_correct"
    # 题目基础分 3 只在创建时乘以 2，回答时不会再次重复乘倍数。
    assert correct.output["awarded_points"] == 6
    assert correct.output["total_points"] == 6
    assert repeated_correct.output["provider_status"] == "task_not_found"
    assert ranking.output["ranking"] == [
        {"user_id": "player-alpha", "user_name": "甲玩家", "points": 6},
        {"user_id": "player-beta", "user_name": "乙玩家", "points": 1},
    ]
    assert final_state is not None and final_state["active_tasks"] == {}
    assert forged_state is None
    assert alpha_account is not None and alpha_account.total_points == 6
    assert beta_account is not None and beta_account.total_points == 1
    assert [item.points_delta for item in game_transactions] == [-1, 6]
    assert game_transactions[0].idempotency_key.endswith(f":wrong:{wrong_request.context.message_id}")
    assert game_transactions[1].idempotency_key.endswith(":1:reward")

    untrusted = module.execute_action(PluginExecutionRequest(action="execute", config={}, payload={"content": "/s"}))
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/game_task/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "ROOM_STATES" not in source
    assert "request.payload.get" not in source
    assert 'payload.get("sender_id")' not in source
    assert 'payload.get("conversation_external_id")' not in source


def test_message_push_task_creates_idempotent_text_job_from_trusted_group(message_push_fixture) -> None:
    """定时推送只采用真实群消息的时间和内容，同一消息重复执行只创建一个任务。"""

    factory, identity = message_push_fixture
    discovered = get_discovered_plugin("message_push_task", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("定时推送",)
    assert "telegram" in discovered.capability.channel_types

    loaded = load_directory_plugin("message_push_task", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    schedule_at = (now_shanghai() + timedelta(days=1)).replace(microsecond=0)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        request = _message_push_request(
            db,
            identity,
            content=f"/定时推送 创建 {schedule_at.isoformat()} 明天上午开会",
            payload={
                "conversation_id": identity["other_conversation_id"],
                "content": "伪造内容",
                "message_type": "image",
                "xml": "<forged />",
                "image_path": "/tmp/forged.png",
            },
        )
        first = module.execute_message_action(db, request)
        repeated = module.execute_message_action(db, request)
        jobs = db.scalars(select(TaskJob).order_by(TaskJob.id.asc())).all()

    assert first.ok is True and repeated.ok is True
    assert first.output["provider_status"] == "scheduled"
    assert repeated.output["job_id"] == first.output["job_id"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job.related_type == "message_push_task_binding"
    assert job.related_id == identity["binding_id"]
    assert job.max_attempts == 1
    assert job.dedupe_key and f":{identity['conversation_id']}:" in job.dedupe_key
    payload = json.loads(job.payload_json)
    assert payload["plugin_code"] == "message_push_task"
    assert payload["conversation_id"] == identity["conversation_id"]
    assert payload["payload"] == {
        "external_id": "push-room",
        "receiver_id": "push-room",
        "chat_type": "group",
        "message_type": "text",
        "content": "明天上午开会",
    }
    assert "forged" not in job.payload_json

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={},
            payload={
                "conversation_id": identity["conversation_id"],
                "schedule_at": schedule_at.isoformat(),
                "content": "伪造推送",
            },
        )
    )
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/message_push_task/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "request.payload.get" not in source
    assert 'payload.get("external_id")' not in source
    assert 'payload.get("channel_id")' not in source


def test_message_push_task_lists_and_cancels_only_current_conversation(message_push_fixture) -> None:
    """列表和取消同时校验当前插件绑定、插件代码与群会话，拒绝跨群任务。"""

    factory, identity = message_push_fixture
    loaded = load_directory_plugin("message_push_task", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    schedule_at = (now_shanghai() + timedelta(days=1)).replace(microsecond=0)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        created = module.execute_message_action(
            db,
            _message_push_request(
                db,
                identity,
                content=f"定时推送 创建 {schedule_at.isoformat()} 当前群提醒",
            ),
        )
        current_job_id = int(created.output["job_id"])
        # 构造同一绑定下的其他群任务和同群其他插件任务，验证查询不会因为
        # related_id 相同而越过会话及 plugin_code 边界。
        other_conversation_job = TaskJob(
            tenant_id=identity["tenant_id"],
            job_type="outbound_dispatch",
            status="pending",
            priority=80,
            max_attempts=1,
            available_at=schedule_at,
            related_type="message_push_task_binding",
            related_id=identity["binding_id"],
            payload_json=json.dumps(
                {
                    "channel_id": identity["channel_id"],
                    "conversation_id": identity["other_conversation_id"],
                    "plugin_code": "message_push_task",
                    "payload": {"message_type": "text", "content": "其他群提醒"},
                },
                ensure_ascii=False,
            ),
        )
        other_plugin_job = TaskJob(
            tenant_id=identity["tenant_id"],
            job_type="outbound_dispatch",
            status="pending",
            priority=80,
            max_attempts=1,
            available_at=schedule_at,
            related_type="message_push_task_binding",
            related_id=identity["binding_id"],
            payload_json=json.dumps(
                {
                    "channel_id": identity["channel_id"],
                    "conversation_id": identity["conversation_id"],
                    "plugin_code": "other_plugin",
                    "payload": {"message_type": "text", "content": "其他插件提醒"},
                },
                ensure_ascii=False,
            ),
        )
        db.add_all([other_conversation_job, other_plugin_job])
        db.commit()

        listed = module.execute_message_action(
            db,
            _message_push_request(db, identity, content="定时推送 列表"),
        )
        denied = module.execute_message_action(
            db,
            _message_push_request(db, identity, content=f"定时推送 取消 {other_conversation_job.id}"),
        )
        cancelled = module.execute_message_action(
            db,
            _message_push_request(db, identity, content=f"定时推送 取消 {current_job_id}"),
        )
        current_job = db.get(TaskJob, current_job_id)
        foreign_job = db.get(TaskJob, other_conversation_job.id)

    assert listed.output["provider_status"] == "listed"
    assert [item["job_id"] for item in listed.output["tasks"]] == [current_job_id]
    assert denied.ok is False and denied.output["provider_status"] == "task_not_found"
    assert cancelled.ok is True and cancelled.output["provider_status"] == "cancelled"
    assert current_job is not None and current_job.status == "cancelled"
    assert foreign_job is not None and foreign_job.status == "pending"


def test_message_sign_uses_trusted_group_identity_and_idempotent_reward(message_sign_fixture) -> None:
    """签到只采用真实消息发送人和群会话，重复执行同一消息不重复发积分。"""

    factory, identity = message_sign_fixture
    discovered = get_discovered_plugin("message_sign", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("签到", "补签")
    assert discovered.capability.chat_types == ("group",)
    assert "telegram" in discovered.capability.channel_types

    loaded = load_directory_plugin("message_sign", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        request = _message_sign_request(
            db,
            identity,
            content="/签到",
            sender_id="sign-user",
            sender_name="签到用户",
            payload={
                "command_name": "补签",
                "sender_id": "forged-user",
                "sender_name": "伪造昵称",
                "conversation_id": identity["other_conversation_id"],
                "conversation_external_id": "other-sign-room",
            },
        )
        first = module.execute_message_action(db, request)
        repeated = module.execute_message_action(db, request)
        record = db.scalar(
            select(SignInRecord).where(
                SignInRecord.user_id == "sign-user",
                SignInRecord.group_id == "sign-room",
            )
        )
        histories = db.scalars(
            select(SignInHistory).where(
                SignInHistory.user_id == "sign-user",
                SignInHistory.group_id == "sign-room",
            )
        ).all()
        transactions = db.scalars(
            select(PointsTransaction).where(
                PointsTransaction.user_id == "sign-user",
                PointsTransaction.group_id == "sign-room",
            )
        ).all()
        forged_record = db.scalar(
            select(SignInRecord).where(
                (SignInRecord.user_id == "forged-user")
                | (SignInRecord.group_id == "other-sign-room")
            )
        )

    assert first.ok is True and first.output["provider_status"] == "signed"
    assert first.output["points_awarded"] == 4
    assert first.output["signin_streak"] == 1
    assert repeated.ok is True and repeated.output["provider_status"] == "already_signed"
    assert record is not None
    assert record.user_name == "签到用户"
    assert record.points == 4
    assert record.total_sign_count == 1
    assert record.signin_streak == 1
    assert len(histories) == 1 and histories[0].points_delta == 4
    assert len(transactions) == 1 and transactions[0].points_delta == 4
    assert transactions[0].idempotency_key and transactions[0].idempotency_key.endswith(
        f":{request.context.message_id}:reward"
    )
    assert forged_record is None

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={},
            payload={
                "sender_id": "sign-user",
                "conversation_external_id": "sign-room",
                "content": "签到",
            },
        )
    )
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/message_sign/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "request.payload.get" not in source
    assert 'payload.get("sender_id")' not in source
    assert 'payload.get("conversation_external_id")' not in source


def test_message_sign_runs_on_ai_rewritten_command_without_persisting_it(message_sign_fixture) -> None:
    """AI 把“我要签到”改写成 /签到：插件必须真的签到成功，而库里的原消息保持用户原话。

    这是线上暴露的缺陷：插件按消息 ID 回库读正文，只改事件正文会得到 command_invalid。
    """

    from app.services.ai_dispatch import effective_message_content

    factory, identity = message_sign_fixture
    loaded = load_directory_plugin("message_sign", force=True)
    module = loaded.instance._module

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        request = _message_sign_request(
            db, identity, content="@机器人 我要签到", sender_id="ai-user", sender_name="AI 用户"
        )
        message_id = int(request.context.message_id)
        # 不改写时，原话解析不出命令。
        assert module.execute_message_action(db, request).ok is False
        with effective_message_content(db, message_id, "/签到"):
            result = module.execute_message_action(db, request)
            db.flush()
        assert result.ok is True, result.message
        assert db.get(Message, message_id).content == "@机器人 我要签到"
        db.commit()
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        assert db.get(Message, message_id).content == "@机器人 我要签到"
        assert db.scalar(select(SignInRecord).where(SignInRecord.user_id == "ai-user")) is not None


def test_message_sign_makeup_restores_full_streak_and_deducts_once(message_sign_fixture) -> None:
    """今天已签到时补昨天会计入昨天和今天，补签成本只在积分账本扣除一次。"""

    factory, identity = message_sign_fixture
    loaded = load_directory_plugin("message_sign", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    current = now_shanghai()
    today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    day_before_yesterday = today_start - timedelta(days=2)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        record = SignInRecord(
            user_id="makeup-user",
            group_id="sign-room",
            user_name="补签用户",
            points=28,
            total_sign_count=3,
            signin_streak=1,
            previous_streak=2,
            sign_stat=current,
            last_sign_date=day_before_yesterday,
        )
        db.add(record)
        PointsService(db).add_points(
            "makeup-user",
            "sign-room",
            28,
            user_name="补签用户",
            source="test_seed",
            idempotency_key="test-seed:message-sign:makeup-user",
        )
        request = _message_sign_request(
            db,
            identity,
            content="#补签",
            sender_id="makeup-user",
            sender_name="补签用户",
            payload={
                "command_name": "签到",
                "sender_id": "forged-user",
                "conversation_external_id": "other-sign-room",
            },
        )
        first = module.execute_message_action(db, request)
        repeated = module.execute_message_action(db, request)
        final_record = db.scalar(
            select(SignInRecord).where(
                SignInRecord.user_id == "makeup-user",
                SignInRecord.group_id == "sign-room",
            )
        )
        makeup_histories = db.scalars(
            select(SignInHistory).where(
                SignInHistory.user_id == "makeup-user",
                SignInHistory.group_id == "sign-room",
                SignInHistory.is_makeup.is_(True),
            )
        ).all()
        makeup_transactions = db.scalars(
            select(PointsTransaction).where(
                PointsTransaction.user_id == "makeup-user",
                PointsTransaction.group_id == "sign-room",
                PointsTransaction.source == "checkin_makeup",
            )
        ).all()
        account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.user_id == "makeup-user",
                PointsAccount.group_id == "sign-room",
            )
        )

    assert first.ok is True and first.output["provider_status"] == "makeup_signed"
    assert first.output["signin_streak"] == 4
    assert first.output["total_sign_count"] == 4
    assert first.output["points"] == 18
    assert repeated.ok is False and repeated.output["provider_status"] == "makeup_unavailable"
    assert final_record is not None
    assert final_record.points == 28
    assert final_record.signin_streak == 4
    assert final_record.previous_streak == 0
    assert final_record.total_sign_count == 4
    assert len(makeup_histories) == 1
    assert makeup_histories[0].points_delta == 0
    assert len(makeup_transactions) == 1
    assert makeup_transactions[0].points_delta == -10
    assert makeup_transactions[0].idempotency_key and makeup_transactions[0].idempotency_key.endswith(
        f":{request.context.message_id}:makeup"
    )
    assert account is not None and account.total_points == 18


def test_point_trade_uses_trusted_group_target_and_idempotent_transfer(point_trade_fixture) -> None:
    """转账目标从当前群成员解析，载荷伪造字段和重复消息不能改变交易结果。"""

    factory, identity = point_trade_fixture
    discovered = get_discovered_plugin("point_trade", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("积分转账", "我的积分", "积分排行", "打劫", "保释")
    assert "telegram" in discovered.capability.channel_types

    loaded = load_directory_plugin("point_trade", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        service = PointsService(db)
        service.add_points(
            "trade-alpha",
            "trade-room",
            50,
            user_name="甲",
            source="test_seed",
            idempotency_key="test-seed:point-trade:alpha",
        )
        service.add_points(
            "trade-beta",
            "trade-room",
            5,
            user_name="乙",
            source="test_seed",
            idempotency_key="test-seed:point-trade:beta",
        )
        request = _point_trade_request(
            db,
            identity,
            content="/积分转账 12 @乙",
            sender_id="trade-alpha",
            sender_name="甲",
            payload={
                "amount": 999,
                "target_user_id": "trade-gamma",
                "target_user_name": "伪造目标",
                "sender_id": "forged-user",
                "conversation_external_id": "other-trade-room",
            },
        )
        first = module.execute_message_action(db, request)
        repeated = module.execute_message_action(db, request)
        detail = module.execute_message_action(
            db,
            _point_trade_request(
                db,
                identity,
                content="我的积分",
                sender_id="trade-alpha",
                sender_name="甲",
            ),
        )
        ranking = module.execute_message_action(
            db,
            _point_trade_request(
                db,
                identity,
                content="积分排行",
                sender_id="trade-alpha",
                sender_name="甲",
            ),
        )
        alpha = db.scalar(
            select(PointsAccount).where(
                PointsAccount.user_id == "trade-alpha",
                PointsAccount.group_id == "trade-room",
            )
        )
        beta = db.scalar(
            select(PointsAccount).where(
                PointsAccount.user_id == "trade-beta",
                PointsAccount.group_id == "trade-room",
            )
        )
        trade_transactions = db.scalars(
            select(PointsTransaction).where(
                PointsTransaction.group_id == "trade-room",
                PointsTransaction.source == "trade",
            )
        ).all()
        forged = db.scalar(
            select(PointsAccount).where(
                (PointsAccount.user_id == "trade-gamma")
                | (PointsAccount.group_id == "other-trade-room")
            )
        )

    assert first.ok is True and first.output["provider_status"] == "transfer_completed"
    assert repeated.ok is True and repeated.output["provider_status"] == "transfer_completed"
    assert first.output["amount"] == 12
    assert detail.output["provider_status"] == "points_detail"
    assert detail.output["account"]["total_points"] == 38
    assert ranking.output["provider_status"] == "ranking"
    assert [item["user_id"] for item in ranking.output["ranking"]] == ["trade-alpha", "trade-beta"]
    assert alpha is not None and alpha.total_points == 38
    assert beta is not None and beta.total_points == 17
    assert len(trade_transactions) == 2
    assert {item.points_delta for item in trade_transactions} == {-12, 12}
    assert all(item.idempotency_key and f":{request.context.message_id}:transfer:" in item.idempotency_key for item in trade_transactions)
    assert forged is None

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={},
            payload={
                "sender_id": "trade-alpha",
                "target_user_id": "trade-beta",
                "amount": 12,
                "content": "积分转账 12 @乙",
            },
        )
    )
    assert untrusted.output["provider_status"] == "trusted_context_required"
    source = (PROJECT_ROOT / "plugins/point_trade/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "request.payload.get" not in source
    assert 'payload.get("target_user_id")' not in source
    assert "random.random" not in source


def test_point_trade_robbery_success_is_atomic_stable_and_cooled_down(point_trade_fixture) -> None:
    """打劫成功一次性结算目标、打劫者和系统账户，并使用持久冷却阻止重复消息。"""

    factory, identity = point_trade_fixture
    loaded = load_directory_plugin("point_trade", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        service = PointsService(db)
        service.add_points(
            "trade-alpha",
            "trade-room",
            100,
            user_name="甲",
            source="test_seed",
            idempotency_key="test-seed:point-trade:robber-success",
        )
        service.add_points(
            "trade-beta",
            "trade-room",
            100,
            user_name="乙",
            source="test_seed",
            idempotency_key="test-seed:point-trade:target-success",
        )
        request = _point_trade_request(
            db,
            identity,
            content="打劫 @乙",
            sender_id="trade-alpha",
            sender_name="甲",
            config={
                "rob_success_rate": 1.0,
                "rob_min_percent": 0.2,
                "rob_max_percent": 0.2,
                "rob_cooldown": 300,
            },
        )
        first = module.execute_message_action(db, request)
        repeated = module.execute_message_action(db, request)
        accounts = {
            item.user_id: item
            for item in db.scalars(
                select(PointsAccount).where(PointsAccount.group_id == "trade-room")
            ).all()
        }
        robbery_transactions = db.scalars(
            select(PointsTransaction).where(
                PointsTransaction.group_id == "trade-room",
                PointsTransaction.source.in_(("rob", "rob_tax")),
            )
        ).all()
        cooldown_states = db.scalars(
            select(PluginStateRecord).where(
                PluginStateRecord.bot_plugin_id == identity["binding_id"],
                PluginStateRecord.namespace == "point_trade",
            )
        ).all()

    assert first.ok is True and first.output["provider_status"] == "rob_success"
    assert first.output["robbed_points"] == 20
    assert first.output["amount"] == 16
    assert first.output["tax_amount"] == 4
    assert repeated.ok is False and repeated.output["provider_status"] == "rob_cooldown"
    assert accounts["trade-alpha"].total_points == 116
    assert accounts["trade-beta"].total_points == 80
    assert accounts["SYSTEM"].total_points == 4
    assert len(robbery_transactions) == 3
    assert sorted(item.points_delta for item in robbery_transactions) == [-20, 4, 16]
    assert len(cooldown_states) == 1


def test_point_trade_robbery_failure_and_bailout_are_atomic(point_trade_fixture) -> None:
    """打劫失败罚款与入狱同事务提交，保释重复执行只扣一次费用。"""

    factory, identity = point_trade_fixture
    loaded = load_directory_plugin("point_trade", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        service = PointsService(db)
        for user_id, name, points in (
            ("trade-gamma", "丙", 100),
            ("trade-beta", "乙", 100),
            ("trade-delta", "丁", 50),
        ):
            service.add_points(
                user_id,
                "trade-room",
                points,
                user_name=name,
                source="test_seed",
                idempotency_key=f"test-seed:point-trade:{user_id}",
            )
        failed = module.execute_message_action(
            db,
            _point_trade_request(
                db,
                identity,
                content="打劫 @乙",
                sender_id="trade-gamma",
                sender_name="丙",
                config={"rob_success_rate": 0.0, "rob_penalty_percent": 0.1, "rob_cooldown": 0},
            ),
        )
        bailout_request = _point_trade_request(
            db,
            identity,
            content="保释 @丙",
            sender_id="trade-delta",
            sender_name="丁",
        )
        bailed = module.execute_message_action(db, bailout_request)
        repeated_bailout = module.execute_message_action(db, bailout_request)
        accounts = {
            item.user_id: item
            for item in db.scalars(
                select(PointsAccount).where(PointsAccount.group_id == "trade-room")
            ).all()
        }
        prison = db.scalar(
            select(PointsPrisonRecord).where(
                PointsPrisonRecord.user_id == "trade-gamma",
                PointsPrisonRecord.group_id == "trade-room",
            )
        )
        penalty_transactions = db.scalars(
            select(PointsTransaction).where(
                PointsTransaction.group_id == "trade-room",
                PointsTransaction.source == "rob_penalty",
            )
        ).all()
        bailout_transactions = db.scalars(
            select(PointsTransaction).where(
                PointsTransaction.group_id == "trade-room",
                PointsTransaction.source == "bailout",
            )
        ).all()

    assert failed.ok is True and failed.output["provider_status"] == "rob_failed"
    assert failed.output["penalty_amount"] == 10
    assert bailed.ok is True and bailed.output["provider_status"] == "bailout_success"
    assert repeated_bailout.ok is True and repeated_bailout.output["provider_status"] == "bailout_success"
    assert accounts["trade-gamma"].total_points == 90
    assert accounts["trade-beta"].total_points == 110
    assert accounts["trade-delta"].total_points == 20
    assert prison is not None and prison.status == "bailed_out" and prison.bailed_out_by == "trade-delta"
    assert len(penalty_transactions) == 2
    assert sorted(item.points_delta for item in penalty_transactions) == [-10, 10]
    assert len(bailout_transactions) == 1 and bailout_transactions[0].points_delta == -BAILOUT_COST
    assert bailout_transactions[0].idempotency_key and bailout_transactions[0].idempotency_key.endswith(
        f":{bailout_request.context.message_id}:bailout"
    )


def test_game_task_scheduled_assignment_queues_one_trusted_outbound_job(game_task_fixture, monkeypatch) -> None:
    """随机发题只使用配置会话和已报名玩家，并通过通用出站队列发送。"""

    factory, identity = game_task_fixture
    loaded = load_directory_plugin("game_task", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module.random, "choice", lambda items: items[0])

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        module.execute_message_action(
            db,
            _game_message_request(
                db,
                identity,
                content="/s",
                sender_id="scheduled-player",
                sender_name="定时玩家",
            ),
        )
        context = PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            channel_type="telegram",
            chat_type="group",
        )
        result = module.execute_scheduled_action(
            db,
            PluginExecutionRequest(
                action="random_task_assignment",
                config={
                    "random_task_enabled": True,
                    "random_task_conversation_ids": [identity["conversation_id"]],
                },
                payload={"conversation_id": identity["forged_conversation_id"]},
                context=context,
            ),
        )
        jobs = db.scalars(select(TaskJob).order_by(TaskJob.id.asc())).all()
        state = PluginStateStore(db, context).get(
            "game_task",
            f"conversation:{identity['conversation_id']}",
        )

    assert result.ok is True
    assert result.output["provider_status"] == "task_assigned"
    assert result.output["conversation_id"] == identity["conversation_id"]
    assert len(jobs) == 1
    assert jobs[0].related_type == "game_task_binding"
    job_payload = json.loads(jobs[0].payload_json)
    assert job_payload["conversation_id"] == identity["conversation_id"]
    assert job_payload["payload"]["receiver_id"] == "game-room"
    assert "forged-room" not in jobs[0].payload_json
    assert state is not None
    assert state["active_tasks"]["1"]["holder_id"] == "scheduled-player"


def test_jd_sign_token_uses_private_message_secret_config_and_bounded_http(jd_token_fixture, monkeypatch) -> None:
    """京东 Cookie 只从真实私聊读取，青龙凭证来自密文配置且结果不回显完整账号。"""

    factory, identity = jd_token_fixture
    discovered = get_discovered_plugin("jd_sign_token", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("设置京东",)
    assert discovered.capability.chat_types == ("direct",)
    assert "telegram" in discovered.capability.channel_types
    assert discovered.secret_fields == ("client_id", "client_secret")
    assert discovered.runtime_config["ql_host"] == ""

    loaded = load_directory_plugin("jd_sign_token", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class FakeQlClient:
        """记录受控 HTTP 参数，并返回一条同 pt_pin 的现有青龙环境变量。"""

        def __init__(self):
            self.calls: list[dict[str, object]] = []

        def request(self, method, url, **kwargs):  # noqa: ANN001
            self.calls.append({"method": method, "url": url, **kwargs})
            if "/open/auth/token" in url:
                payload = {"code": 200, "data": {"token_type": "Bearer", "token": "ql-access-token"}}
            elif method == "GET" and "/open/envs" in url:
                payload = {
                    "code": 200,
                    "data": [
                        {
                            "id": "env-001",
                            "name": "JD_COOKIE",
                            "value": "pt_key=old-key;pt_pin=jd_pin_001;",
                            "remarks": "旧备注",
                        }
                    ],
                }
            elif method == "PUT" and "/open/envs" in url:
                payload = {"code": 200, "data": kwargs.get("json_body")}
            else:
                raise AssertionError(f"unexpected ql request: {method} {url}")
            return PluginHttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                url=url,
            )

    fake_client = FakeQlClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_client)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        request = _jd_message_request(
            db,
            identity,
            content="/设置京东 pt_key=new-key;pt_pin=jd_pin_001; 新备注",
            payload={
                "token": "pt_key=forged;pt_pin=forged_pin;",
                "remark": "伪造备注",
                "sender_id": "forged-user",
            },
        )
        result = module.execute_message_action(db, request)

    assert result.ok is True
    assert result.output == {
        "provider_status": "updated",
        "env_name": "JD_COOKIE",
        "remark": "新备注",
        "account_hint": "jd***001",
        "replies": [
            {
                "type": "text",
                "content": "已更新京东账号“新备注”，账号标识：jd***001。",
            }
        ],
    }
    assert len(fake_client.calls) == 3
    auth_call, env_call, update_call = fake_client.calls
    assert auth_call["method"] == "GET"
    assert "client_secret=tenant-client-secret" in str(auth_call["url"])
    assert env_call["method"] == "GET"
    assert "searchValue=JD_COOKIE" in str(env_call["url"])
    assert update_call["method"] == "PUT"
    assert update_call["json_body"] == {
        "id": "env-001",
        "name": "JD_COOKIE",
        "value": "pt_key=new-key;pt_pin=jd_pin_001;",
        "remarks": "新备注",
    }
    assert update_call["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer ql-access-token",
    }
    assert all(call["max_response_bytes"] == 2 * 1024 * 1024 for call in fake_client.calls)
    serialized_result = json.dumps(dict(result.output), ensure_ascii=False)
    assert "new-key" not in serialized_result
    assert "jd_pin_001" not in serialized_result
    assert "tenant-client-secret" not in serialized_result
    assert "forged_pin" not in json.dumps(update_call["json_body"], ensure_ascii=False)

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={
                "ql_host": "https://ql.example.test",
                "client_id": "id",
                "client_secret": "secret",
            },
            payload={"token": "pt_key=x;pt_pin=y;"},
        )
    )
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/jd_sign_token/plugin.py").read_text(encoding="utf-8")
    assert "urllib.request" not in source
    assert "urlopen" not in source
    assert "request.payload.get" not in source
    assert "qrcode.png" not in source
    assert "except Exception" not in source


def test_lucky_pot_persists_room_and_keeps_draw_rewards_idempotent(lucky_pot_fixture, monkeypatch) -> None:
    """满员开奖使用可信群状态和幂等流水，奖励总额不超过扣除抽水后的奖池。"""

    factory, identity = lucky_pot_fixture
    discovered = get_discovered_plugin("lucky_pot", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("大奖池",)
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config["countdown_seconds"] == 120

    loaded = load_directory_plugin("lucky_pot", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(
        module,
        "_distribute_rewards",
        lambda player_ids, total_points: {player_ids[0]: 5, player_ids[1]: total_points - 5},
    )

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        service = PointsService(db)
        service.add_points(
            "player-a",
            "lucky-room",
            100,
            user_name="甲玩家",
            source="test_seed",
            idempotency_key="test-seed:lucky:a",
        )
        service.add_points(
            "player-b",
            "lucky-room",
            100,
            user_name="乙玩家",
            source="test_seed",
            idempotency_key="test-seed:lucky:b",
        )
        create_request = _lucky_message_request(
            db,
            identity,
            content="/大奖池 创建",
            sender_id="player-a",
            sender_name="甲玩家",
            payload={
                "command_args": "取消",
                "sender_id": "forged-user",
                "conversation_external_id": "forged-room",
            },
        )
        created = module.execute_message_action(db, create_request)
        context = create_request.context
        assert context is not None
        store = PluginStateStore(db, context)
        state_key = f"conversation:{identity['conversation_id']}"
        created_room = store.get("lucky_pot", state_key)
        scheduled_jobs = db.scalars(
            select(TaskJob).where(TaskJob.related_type == "plugin_action:draw_pot")
        ).all()

        joined = module.execute_message_action(
            db,
            _lucky_message_request(
                db,
                identity,
                content="#大奖池 参与",
                sender_id="player-b",
                sender_name="乙玩家",
            ),
        )
        repeated_join = module.execute_message_action(
            db,
            _lucky_message_request(
                db,
                identity,
                content="大奖池 参与",
                sender_id="player-b",
                sender_name="乙玩家",
            ),
        )
        final_room = store.get("lucky_pot", state_key)
        accounts = db.scalars(
            select(PointsAccount)
            .where(PointsAccount.group_id == "lucky-room")
            .order_by(PointsAccount.user_id.asc())
        ).all()
        transactions = db.scalars(
            select(PointsTransaction)
            .where(PointsTransaction.source.in_(("lucky_pot_entry", "lucky_pot_reward")))
            .order_by(PointsTransaction.id.asc())
        ).all()

    assert created.output["provider_status"] == "created"
    assert created_room is not None
    assert created_room["group_id"] == "lucky-room"
    assert created_room["players"] == {"player-a": "甲玩家"}
    assert len(scheduled_jobs) == 1
    assert joined.output["provider_status"] == "drawn"
    assert joined.output["total_pot"] == 20
    assert joined.output["house_cut"] == 2
    assert "rewards" not in joined.output
    assert repeated_join.output["provider_status"] == "room_missing"
    assert final_room is None
    balances = {item.user_id: item.total_points for item in accounts}
    assert balances == {"player-a": 95, "player-b": 103}
    assert sum(balances.values()) == 198
    assert [item.points_delta for item in transactions] == [-10, -10, 5, 13]
    assert len({item.idempotency_key for item in transactions}) == 4
    assert all("forged" not in str(item.idempotency_key) for item in transactions)

    source = (PROJECT_ROOT / "plugins/lucky_pot/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "ACTIVE_ROOMS" not in source
    assert "request.payload.get" not in source
    assert 'payload.get("sender_id")' not in source
    assert 'payload.get("conversation_external_id")' not in source


def test_lucky_pot_scheduled_action_refunds_when_players_are_insufficient(lucky_pot_fixture) -> None:
    """倒计时结束仍不足最低人数时自动退款，并向可信群创建通用出站任务。"""

    factory, identity = lucky_pot_fixture
    loaded = load_directory_plugin("lucky_pot", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        PointsService(db).add_points(
            "player-a",
            "lucky-room",
            100,
            user_name="甲玩家",
            source="test_seed",
            idempotency_key="test-seed:lucky:refund",
        )
        create_request = _lucky_message_request(
            db,
            identity,
            content="大奖池 创建",
            sender_id="player-a",
            sender_name="甲玩家",
            max_players=3,
        )
        created = module.execute_message_action(db, create_request)
        context = PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            channel_type="telegram",
            chat_type="group",
        )
        refunded = module.execute_scheduled_action(
            db,
            PluginExecutionRequest(action="draw_pot", config={}, context=context),
        )
        account = db.scalar(
            select(PointsAccount).where(
                PointsAccount.user_id == "player-a",
                PointsAccount.group_id == "lucky-room",
            )
        )
        transactions = db.scalars(
            select(PointsTransaction)
            .where(PointsTransaction.source.in_(("lucky_pot_entry", "lucky_pot_refund")))
            .order_by(PointsTransaction.id.asc())
        ).all()
        state = PluginStateStore(db, context).get(
            "lucky_pot",
            f"conversation:{identity['conversation_id']}",
        )
        outbound_jobs = db.scalars(
            select(TaskJob).where(TaskJob.related_type == "lucky_pot_binding")
        ).all()

    assert created.output["provider_status"] == "created"
    assert refunded.output["provider_status"] == "refunded"
    assert refunded.output["refunded_player_count"] == 1
    assert account is not None and account.total_points == 100
    assert [item.points_delta for item in transactions] == [-10, 10]
    assert state is None
    assert len(outbound_jobs) == 1
    outbound_payload = json.loads(outbound_jobs[0].payload_json)
    assert outbound_payload["conversation_id"] == identity["conversation_id"]
    assert outbound_payload["payload"]["receiver_id"] == "lucky-room"
    assert "人数不足" in outbound_payload["payload"]["content"]


def test_points_context_entry_unifies_earn_and_spend_into_one_account(game_task_fixture) -> None:
    """统一口径:不同插件按上下文赚取和消费,落在同一个 租户+群+用户 账户;换群则是另一个账户。"""

    from app.models.entities import Message
    from app.services.plugin_runtime.contracts import PluginExecutionContext

    factory, identity = game_task_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        message = Message(
            tenant_id=identity["tenant_id"],
            conversation_id=identity["conversation_id"],
            direction="inbound",
            sender_id="wxid_a",
            sender_name="小A",
            content="签到",
        )
        db.add(message)
        db.flush()

        def ctx(conversation_id: int) -> PluginExecutionContext:
            return PluginExecutionContext(
                tenant_id=identity["tenant_id"],
                binding_id=identity["binding_id"],
                bot_id=identity["bot_id"],
                channel_id=identity["channel_id"],
                conversation_id=conversation_id,
                message_id=int(message.id),
                sender_id="wxid_a",
                chat_type="group",
            )

        service = PointsService(db)
        service.earn(ctx(identity["conversation_id"]), 50, source="checkin", description="签到")
        spent = service.spend(ctx(identity["conversation_id"]), 20, source="demo_plugin", description="番号查询")
        assert spent.ok is True
        assert service.balance(ctx(identity["conversation_id"])) == 30
        # 同一用户在另一个群没有积分,账户按群隔离。
        assert service.balance(ctx(identity["forged_conversation_id"])) == 0
        assert service.spend(ctx(identity["forged_conversation_id"]), 1, source="x", description="x").ok is False
        accounts = db.scalars(select(PointsAccount).where(PointsAccount.user_id == "wxid_a")).all()
        assert [(a.group_id, a.user_name, a.total_points) for a in accounts if a.total_points] == [("game-room", "小A", 30)]


def test_points_account_name_is_backfilled_from_wxid_to_nickname(tmp_path: Path) -> None:
    """账户昵称此前只存了 wxid 时,拿到真实昵称后应回填;已有真实昵称不被覆盖。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'points-name.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with tenant_scope(1), tenant_session(factory) as db:
            service = PointsService(db)
            service.add_points("wxid_abc123", "g@chatroom", 5, user_name="wxid_abc123", source="t")
            service.add_points("wxid_abc123", "g@chatroom", 5, user_name="小明", source="t")
            service.add_points("wxid_abc123", "g@chatroom", 5, user_name="另一个名字", source="t")
            account = db.scalar(select(PointsAccount).where(PointsAccount.user_id == "wxid_abc123"))
            assert account.user_name == "小明" and account.total_points == 15
            assert service.group_balances("g@chatroom") == {"wxid_abc123": 15}
            assert service.user_totals(["wxid_abc123", "nobody"]) == {"wxid_abc123": 15}
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
