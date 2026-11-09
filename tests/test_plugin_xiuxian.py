from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-xiuxian"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.cultivation import CultivationPlayer, CultivationTransaction
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Message, Plugin
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest


@pytest.fixture()
def xiuxian_fixture(tmp_path: Path):
    """建立两个租户的同名插件资源，用于验证可信主键和租户隔离。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'xiuxian.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        alpha = Tenant(code="xiuxian-alpha", name="甲修仙租户", status="active")
        beta = Tenant(code="xiuxian-beta", name="乙修仙租户", status="active")
        plugin = Plugin(code="xiuxian", display_name="文字修仙", plugin_type="command")
        db.add_all([alpha, beta, plugin])
        db.flush()

        identity: dict[str, int] = {}
        for prefix, tenant, sender_id, sender_name, dao_name in (
            ("alpha", alpha, "alpha-user", "甲玩家", "青云子"),
            ("beta", beta, "beta-user", "乙玩家", "玄真子"),
        ):
            bot = Bot(tenant_id=tenant.id, name=f"{prefix}修仙机器人", status="running", is_enabled=True)
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
                name=f"{prefix} Telegram",
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
                external_id=f"{prefix}-cultivation-group",
                title=f"{prefix}修仙群",
                chat_type="group",
            )
            db.add(conversation)
            db.flush()
            message = Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction="in",
                sender_id=sender_id,
                sender_name=sender_name,
                content=f"注册修仙 {dao_name}",
                message_type="text",
                status="received",
            )
            db.add(message)
            db.flush()
            identity.update(
                {
                    f"{prefix}_tenant_id": int(tenant.id),
                    f"{prefix}_bot_id": int(bot.id),
                    f"{prefix}_binding_id": int(binding.id),
                    f"{prefix}_channel_id": int(channel.id),
                    f"{prefix}_conversation_id": int(conversation.id),
                    f"{prefix}_message_id": int(message.id),
                }
            )
        db.commit()
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(identity: dict[str, int], prefix: str = "alpha") -> PluginExecutionRequest:
    """构造只包含平台可信主键的修仙消息执行请求。"""

    return PluginExecutionRequest(
        action="execute",
        config={"enabled": True, "register_cooldown_seconds": 0},
        payload={"content": "注册修仙 伪造道号", "sender_id": "forged-user"},
        context=PluginExecutionContext(
            tenant_id=identity[f"{prefix}_tenant_id"],
            binding_id=identity[f"{prefix}_binding_id"],
            bot_id=identity[f"{prefix}_bot_id"],
            channel_id=identity[f"{prefix}_channel_id"],
            conversation_id=identity[f"{prefix}_conversation_id"],
            message_id=identity[f"{prefix}_message_id"],
            sender_id=f"{prefix}-user",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def test_xiuxian_manifest_is_channel_neutral() -> None:
    """修仙目录声明支持通用渠道，不包含 Provider 地址、凭证或旧出售命令。"""

    discovered = get_discovered_plugin("xiuxian", force=True)

    assert discovered is not None
    assert discovered.is_valid is True
    assert discovered.manifest.plugin_type == "command"
    assert set(discovered.capability.channel_types) == {
        "wechat_ipad",
        "wechat_winauto",
        "openclaw",
        "simulator",
        "telegram",
        "wechat_webridge",
        "workwechat",
    }
    assert "注册修仙" in discovered.matcher.commands
    assert "炼丹" in discovered.matcher.commands
    assert "出售" not in discovered.matcher.commands
    assert discovered.secret_fields == ()


def test_xiuxian_matcher_only_accepts_formal_group_text_commands() -> None:
    """私聊、非文本和已删除旧命令不会进入修仙数据库执行链。"""

    loaded = load_directory_plugin("xiuxian", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    group = type(
        "Context",
        (),
        {"binding_config": {"enabled": True}, "chat_type": "group", "message_type": "text", "content": "聚灵 10"},
    )()
    direct = type(
        "Context",
        (),
        {"binding_config": {"enabled": True}, "chat_type": "direct", "message_type": "text", "content": "我的状态"},
    )()
    removed = type(
        "Context",
        (),
        {"binding_config": {"enabled": True}, "chat_type": "group", "message_type": "text", "content": "出售 聚灵符 1"},
    )()

    assert module.matches_event(group)["matched"] is True
    assert module.matches_event(direct)["matched"] is False
    assert module.matches_event(removed)["matched"] is False


def test_xiuxian_uses_real_message_and_replays_same_result(xiuxian_fixture) -> None:
    """注册只读取真实 Message，同一消息重试返回原结果且不重复创建玩家。"""

    factory, identity = xiuxian_fixture
    loaded = load_directory_plugin("xiuxian", force=True)
    assert loaded is not None and loaded.instance is not None
    request = _request(identity)
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        first = loaded.instance.execute_message_action(db, request)
        repeated = loaded.instance.execute_message_action(db, request)
        players = db.scalars(select(CultivationPlayer)).all()
        result_rows = db.scalars(
            select(CultivationTransaction).where(CultivationTransaction.transaction_type == "command_result")
        ).all()

    assert first.ok is True
    assert first.output["provider_status"] == "registered"
    assert repeated.output == first.output
    assert len(players) == 1
    assert players[0].member_external_id == "alpha-user"
    assert players[0].dao_name == "青云子"
    assert len(result_rows) == 1


def test_xiuxian_rejects_cross_tenant_resource_mix(xiuxian_fixture) -> None:
    """甲租户请求拼入乙租户渠道主键时直接拒绝，不回退为当前渠道。"""

    factory, identity = xiuxian_fixture
    loaded = load_directory_plugin("xiuxian", force=True)
    assert loaded is not None and loaded.instance is not None
    normal = _request(identity)
    assert normal.context is not None
    forged = PluginExecutionRequest(
        action=normal.action,
        config=normal.config,
        payload=normal.payload,
        context=PluginExecutionContext(
            tenant_id=normal.context.tenant_id,
            binding_id=normal.context.binding_id,
            bot_id=normal.context.bot_id,
            channel_id=identity["beta_channel_id"],
            conversation_id=normal.context.conversation_id,
            message_id=normal.context.message_id,
            sender_id=normal.context.sender_id,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        result = loaded.instance.execute_message_action(db, forged)
        player = db.scalar(select(CultivationPlayer))

    assert result.ok is False
    assert result.output["provider_status"] == "trusted_context_required"
    assert player is None


def test_xiuxian_status_uses_standard_text_reply(xiuxian_fixture) -> None:
    """注册后状态查询输出平台标准文本，不包含渠道协议字段。"""

    factory, identity = xiuxian_fixture
    loaded = load_directory_plugin("xiuxian", force=True)
    assert loaded is not None and loaded.instance is not None
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        loaded.instance.execute_message_action(db, _request(identity))
        message = db.get(Message, identity["alpha_message_id"])
        assert message is not None
        status_message = Message(
            tenant_id=identity["alpha_tenant_id"],
            conversation_id=identity["alpha_conversation_id"],
            direction="in",
            sender_id="alpha-user",
            sender_name="甲玩家",
            content="我的状态",
            message_type="text",
            status="received",
        )
        db.add(status_message)
        db.flush()
        request = _request(identity)
        assert request.context is not None
        status_request = PluginExecutionRequest(
            action="execute",
            config=request.config,
            context=PluginExecutionContext(
                tenant_id=request.context.tenant_id,
                binding_id=request.context.binding_id,
                bot_id=request.context.bot_id,
                channel_id=request.context.channel_id,
                conversation_id=request.context.conversation_id,
                message_id=int(status_message.id),
                sender_id="alpha-user",
                channel_type="telegram",
                chat_type="group",
                message_type="text",
            ),
        )
        result = loaded.instance.execute_message_action(db, status_request)

    assert result.ok is True
    assert result.output["provider_status"] == "status"
    assert result.output["replies"] == [{"type": "text", "content": result.output["reply_text"]}]
    assert "道号：青云子" in result.output["reply_text"]
    assert "Provider" not in result.output["reply_text"]
