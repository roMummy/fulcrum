from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "existing-group-plugins"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Plugin
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest


@pytest.fixture()
def group_plugin_fixture(tmp_path: Path):
    """建立两个群和确定性成员活跃时间，供群插件验证可信作用域。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'group-plugins.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    current = now_shanghai()
    with platform_session(factory) as db:
        tenant = Tenant(code="group-plugins", name="群插件租户", status="active")
        plugin = Plugin(code="inactive_rank", display_name="潜水排行", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="群插件机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="群插件 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        primary_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="group-primary",
            chat_type="group",
        )
        forged_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="group-forged",
            chat_type="group",
        )
        db.add_all([primary_conversation, forged_conversation])
        db.flush()
        primary_chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            external_id="group-primary",
            name="主测试群",
            status="active",
            is_active=True,
        )
        forged_chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            external_id="group-forged",
            name="伪造目标群",
            status="active",
            is_active=True,
        )
        db.add_all([primary_chatroom, forged_chatroom])
        db.flush()
        db.add_all(
            [
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=primary_chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="never-spoken",
                    nickname="从未发言成员",
                    status="active",
                    last_active_at=None,
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=primary_chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="old-member",
                    nickname="长期潜水成员",
                    status="active",
                    last_active_at=current - timedelta(days=45),
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=primary_chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="recent-member",
                    nickname="近期活跃成员",
                    status="active",
                    last_active_at=current - timedelta(days=2),
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=primary_chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="left-member",
                    nickname="已退群成员",
                    status="left",
                    last_active_at=None,
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=forged_chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="forged-member",
                    nickname="其他群成员",
                    status="active",
                    last_active_at=None,
                ),
            ]
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
            "binding_id": int(binding.id),
            "channel_id": int(channel.id),
            "primary_conversation_id": int(primary_conversation.id),
            "forged_conversation_id": int(forged_conversation.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_inactive_rank_uses_trusted_group_and_current_command(group_plugin_fixture) -> None:
    """潜水排行只查询可信群会话，并删除英文别名和全局数据库入口。"""

    factory, identity = group_plugin_fixture
    discovered = get_discovered_plugin("inactive_rank", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("潜水排行",)
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config == {
        "default_days": 60,
        "default_limit": 10,
        "max_days": 365,
        "max_limit": 50,
    }

    loaded = load_directory_plugin("inactive_rank", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
        channel_id=identity["channel_id"],
        conversation_id=identity["primary_conversation_id"],
        sender_id="member-1",
        channel_type="telegram",
        chat_type="group",
        message_type="text",
    )
    request = PluginExecutionRequest(
        action="execute",
        config=discovered.runtime_config,
        payload={
            "content": "潜水排行 30 2",
            "command_name": "潜水排行",
            "command_args": "30 2",
            "conversation_id": identity["forged_conversation_id"],
            "conversation_external_id": "group-forged",
        },
        context=context,
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, request)

    assert result.ok is True
    assert result.output["provider_status"] == "ranking"
    assert result.output["rank_count"] == 2
    assert "从未发言成员" in result.output["replies"][0]["content"]
    assert "长期潜水成员" in result.output["replies"][0]["content"]
    assert "其他群成员" not in result.output["replies"][0]["content"]
    assert "已退群成员" not in result.output["replies"][0]["content"]
    assert "ranking" not in result.output

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config=discovered.runtime_config,
            payload={"content": "潜水排行", "conversation_external_id": "group-primary"},
        )
    )
    assert untrusted.ok is False
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/inactive_rank/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "conversation_external_id" not in source
    assert 'payload.get("room_id")' not in source
