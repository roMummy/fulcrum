from __future__ import annotations

import json
import os
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
from app.models.entities import (
    Bot,
    BotPlugin,
    Channel,
    Conversation,
    Message,
    Plugin,
    PointsAccount,
    PointsTransaction,
    TaskJob,
)
from app.models.saas import Tenant
from app.services.points import PointsService
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
