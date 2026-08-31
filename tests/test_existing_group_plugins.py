from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
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
            "primary_chatroom_id": int(primary_chatroom.id),
            "forged_chatroom_id": int(forged_chatroom.id),
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


def test_telegram_adapter_normalizes_group_member_change() -> None:
    """Telegram 私有成员状态必须先转换成平台通用事件。"""

    from app.services.channel_adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter()
    channel = Channel(bot_id=1, name="Telegram", channel_type="telegram")
    normalized = adapter.normalize_inbound_payload(
        channel,
        {
            "update_id": 9001,
            "chat_member": {
                "chat": {"id": -10001, "type": "supergroup", "title": "测试群"},
                "from": {"id": 7001, "first_name": "群主"},
                "old_chat_member": {
                    "status": "left",
                    "user": {"id": 8001, "first_name": "新", "last_name": "成员"},
                },
                "new_chat_member": {
                    "status": "member",
                    "user": {"id": 8001, "first_name": "新", "last_name": "成员"},
                },
            },
        },
    )

    assert normalized["event_type"] == "chat_member"
    assert normalized["conversation"]["chat_type"] == "group"
    assert normalized["message"]["message_type"] == "event"
    assert normalized["details"]["member_change"] == {
        "change_type": "join",
        "members": [{"external_id": "8001", "display_name": "新 成员"}],
    }


def test_group_member_change_uses_trusted_group_and_generic_event(group_plugin_fixture) -> None:
    """成员变更只更新可信群，并统一处理结构化事件与微信系统消息。"""

    factory, identity = group_plugin_fixture
    discovered = get_discovered_plugin("group_member_change", force=True)
    assert discovered is not None
    assert "telegram" in discovered.capability.channel_types
    assert "event" in discovered.capability.message_types

    loaded = load_directory_plugin("group_member_change", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
        channel_id=identity["channel_id"],
        conversation_id=identity["primary_conversation_id"],
        sender_id="group-admin",
        channel_type="telegram",
        chat_type="group",
        message_type="event",
    )
    join_request = PluginExecutionRequest(
        action="execute",
        config={},
        payload={
            # 伪造的群定位字段只是普通业务数据，不能改变可信上下文。
            "channel_id": 99999,
            "conversation_external_id": "group-forged",
            "normalized_event": {
                "details": {
                    "member_change": {
                        "change_type": "join",
                        "members": [{"external_id": "member-new", "display_name": "新成员"}],
                    }
                }
            },
        },
        context=context,
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        join_result = module.execute_message_action(db, join_request)
        joined = db.scalar(
            select(ChatroomMember).where(
                ChatroomMember.chatroom_id == identity["primary_chatroom_id"],
                ChatroomMember.member_external_id == "member-new",
            )
        )
        forged = db.scalar(
            select(ChatroomMember).where(
                ChatroomMember.chatroom_id == identity["forged_chatroom_id"],
                ChatroomMember.member_external_id == "member-new",
            )
        )
        db.commit()

    assert join_result.ok is True
    assert join_result.output["provider_status"] == "member_join"
    assert join_result.output["replies"] == [{"type": "text", "content": "欢迎 新成员 加入群聊。"}]
    assert joined is not None and joined.status == "active"
    assert forged is None

    leave_xml = """
    <sysmsg type="sysmsgtemplate">
      <sysmsgtemplate><content_template type="tmpl_type_profile">
        <template><![CDATA["$username$"退出了群聊]]></template>
        <link_list><link name="username"><member>
          <username><![CDATA[member-new]]></username>
          <nickname><![CDATA[新成员]]></nickname>
        </member></link></link_list>
      </content_template></sysmsgtemplate>
    </sysmsg>
    """.strip()
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        leave_result = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config={},
                payload={"content": leave_xml},
                context=context,
            ),
        )
        left = db.scalar(
            select(ChatroomMember).where(
                ChatroomMember.chatroom_id == identity["primary_chatroom_id"],
                ChatroomMember.member_external_id == "member-new",
            )
        )
        db.commit()
    assert leave_result.output["provider_status"] == "member_left"
    assert "replies" not in leave_result.output
    assert left is not None and left.status == "left"

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        removed_result = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config={},
                payload={
                    "normalized_event": {
                        "details": {
                            "member_change": {
                                "change_type": "removed",
                                "members": [{"external_id": "member-removed", "display_name": "被移除成员"}],
                            }
                        }
                    }
                },
                context=context,
            ),
        )
        removed = db.scalar(
            select(ChatroomMember).where(
                ChatroomMember.chatroom_id == identity["primary_chatroom_id"],
                ChatroomMember.member_external_id == "member-removed",
            )
        )
    assert removed_result.output["provider_status"] == "member_removed"
    assert removed is not None and removed.status == "left"

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={},
            payload={"conversation_external_id": "group-primary"},
        )
    )
    assert untrusted.ok is False
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/group_member_change/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert 'payload.get("channel_id")' not in source
    assert 'payload.get("conversation_external_id")' not in source
