from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-robot-menu"

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
    Chatroom,
    Conversation,
    GroupPluginScope,
    Message,
    Plugin,
)
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest


@pytest.fixture()
def robot_menu_fixture(tmp_path: Path):
    """建立启用、停用、群级关闭和定时插件，验证菜单只展示真实可用功能。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'robot-menu.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="robot-menu", name="菜单租户", status="active")
        db.add(tenant)
        db.flush()
        plugins = {
            code: Plugin(code=code, display_name=name, plugin_type=plugin_type)
            for code, name, plugin_type in (
                ("robot_menu", "功能菜单", "command"),
                ("weather", "天气", "rule"),
                ("github_opengraph", "GitHub 链接预览", "rule"),
                ("epic_free", "Epic 免费游戏", "scheduled"),
                ("dify", "Dify 对话", "ai"),
            )
        }
        db.add_all(plugins.values())
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="菜单机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="菜单 iPad",
            channel_type="wechat_ipad",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="wechat_ipad",
            external_id="menu-room@chatroom",
            title="菜单群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            external_id="menu-room@chatroom",
            name="菜单群",
            is_active=True,
        )
        db.add(chatroom)
        db.flush()
        bindings = {
            "robot_menu": BotPlugin(
                tenant_id=tenant.id,
                bot_id=bot.id,
                plugin_id=plugins["robot_menu"].id,
                status="enabled",
            ),
            "weather": BotPlugin(
                tenant_id=tenant.id,
                bot_id=bot.id,
                plugin_id=plugins["weather"].id,
                status="enabled",
                config_json=json.dumps({"commands": ["查天气"], "enabled": True}, ensure_ascii=False),
            ),
            "github_opengraph": BotPlugin(
                tenant_id=tenant.id,
                bot_id=bot.id,
                plugin_id=plugins["github_opengraph"].id,
                status="enabled",
            ),
            "epic_free": BotPlugin(
                tenant_id=tenant.id,
                bot_id=bot.id,
                plugin_id=plugins["epic_free"].id,
                status="enabled",
            ),
            "dify": BotPlugin(
                tenant_id=tenant.id,
                bot_id=bot.id,
                plugin_id=plugins["dify"].id,
                status="disabled",
            ),
        }
        db.add_all(bindings.values())
        db.flush()
        db.add(
            GroupPluginScope(
                tenant_id=tenant.id,
                channel_id=channel.id,
                chatroom_id=chatroom.id,
                bot_plugin_id=bindings["github_opengraph"].id,
                scope_status="manual",
                enabled=False,
            )
        )
        message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction="in",
            sender_id="menu-user",
            sender_name="菜单用户",
            content="菜单",
            message_type="text",
            status="received",
        )
        db.add(message)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "binding_id": int(bindings["robot_menu"].id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "message_id": int(message.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _menu_request(identity: dict[str, int]) -> PluginExecutionRequest:
    return PluginExecutionRequest(
        action="execute",
        config={"enabled": True, "max_items": 30},
        payload={
            "content": "菜单 管理员 列表",
            "bot_id": 999,
            "conversation_external_id": "forged-room",
        },
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=identity["message_id"],
            sender_id="menu-user",
            channel_type="wechat_ipad",
            chat_type="group",
            message_type="text",
        ),
    )


def test_robot_menu_lists_only_current_group_user_features(robot_menu_fixture) -> None:
    """菜单展示启用且可触发的绑定，隐藏定时、停用和群级关闭插件。"""

    factory, identity = robot_menu_fixture
    discovered = get_discovered_plugin("robot_menu", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.matcher.commands == ("菜单", "功能菜单")
    assert discovered.capability.chat_types == ("group",)
    assert discovered.capability.consume_policy == "stop"

    loaded = load_directory_plugin("robot_menu", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, _menu_request(identity))

    assert result.ok is True and result.output["item_count"] == 2
    menu_text = result.output["replies"][0]["content"]
    assert "功能菜单" in menu_text
    assert "天气" in menu_text and "查天气" in menu_text
    assert "Epic 免费游戏" not in menu_text
    assert "GitHub 链接预览" not in menu_text
    assert "Dify 对话" not in menu_text
    assert "管理员" not in menu_text
    assert "群列表" not in menu_text


def test_robot_menu_rejects_untrusted_execution_and_removed_subcommands(robot_menu_fixture) -> None:
    """运维执行不能伪造菜单，旧状态和管理子命令也不再保留。"""

    factory, identity = robot_menu_fixture
    loaded = load_directory_plugin("robot_menu", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={"enabled": True},
            payload={"content": "菜单", "conversation_id": identity["conversation_id"]},
        )
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        message = db.get(Message, identity["message_id"])
        assert message is not None
        message.content = "菜单 状态"
        removed = module.execute_message_action(db, _menu_request(identity))

    assert untrusted.ok is False and untrusted.output["provider_status"] == "trusted_context_required"
    assert removed.ok is False and removed.output["provider_status"] == "command_invalid"
    source = (PROJECT_ROOT / "plugins/robot_menu/plugin.py").read_text(encoding="utf-8")
    assert "RobotMenuRenderTool" not in source
    assert "MessageAutoRevoke" not in source
    assert "GroupBotManager" not in source
    assert "WechatAPIClient" not in source
    assert "send_at_message" not in source
    assert "request.payload.get" not in source
