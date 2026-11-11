from __future__ import annotations

import json
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
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message, Plugin, TaskJob
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
        plugin = Plugin(code="group_virtual", display_name="群虚拟", plugin_type="command")
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
        invite_channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="群邀请 iPad",
            channel_type="wechat_ipad",
            status="online",
            is_enabled=True,
        )
        db.add(invite_channel)
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
        invite_direct_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=invite_channel.id,
            channel_type="wechat_ipad",
            external_id="wxid-direct-user",
            chat_type="direct",
        )
        invite_group_conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=invite_channel.id,
            channel_type="wechat_ipad",
            external_id="invite-room@chatroom",
            chat_type="group",
        )
        db.add_all([invite_direct_conversation, invite_group_conversation])
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
        invite_chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=invite_channel.id,
            external_id="invite-room@chatroom",
            name="产品交流群",
            status="active",
            is_active=True,
        )
        db.add(invite_chatroom)
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
            "invite_channel_id": int(invite_channel.id),
            "invite_direct_conversation_id": int(invite_direct_conversation.id),
            "invite_group_conversation_id": int(invite_group_conversation.id),
            "invite_chatroom_id": int(invite_chatroom.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


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


def test_group_invite_operation_dispatches_through_channel_adapter(group_plugin_fixture, monkeypatch) -> None:
    """群邀请通用服务先校验可信资源，再把动作交给渠道适配器。"""

    from app.services import channel_group_operations

    factory, identity = group_plugin_fixture
    calls: list[dict[str, object]] = []

    class FakeAdapter:
        def invite_group_members(self, channel, *, chatroom_external_id, member_external_ids):
            calls.append(
                {
                    "channel_id": channel.id,
                    "chatroom_external_id": chatroom_external_id,
                    "member_external_ids": member_external_ids,
                }
            )
            return {"ok": True, "status": "sent", "action": "invite_group_members"}

    monkeypatch.setattr(channel_group_operations, "get_channel_adapter", lambda _channel: FakeAdapter())
    context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
        channel_id=identity["invite_channel_id"],
        conversation_id=identity["invite_direct_conversation_id"],
        sender_id="wxid-user",
        channel_type="wechat_ipad",
        chat_type="direct",
        message_type="text",
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = channel_group_operations.invite_group_members(
            db,
            context=context,
            chatroom_id=identity["invite_chatroom_id"],
            member_external_ids=["wxid-user", "wxid-user"],
        )
        rejected = channel_group_operations.invite_group_members(
            db,
            context=context,
            chatroom_id=identity["primary_chatroom_id"],
            member_external_ids=["wxid-user"],
        )

    assert result["ok"] is True
    assert calls == [
        {
            "channel_id": identity["invite_channel_id"],
            "chatroom_external_id": "invite-room@chatroom",
            "member_external_ids": ["wxid-user"],
        }
    ]
    assert rejected["error_code"] == "chatroom_not_found"


def test_wechat_ipad_adapter_implements_generic_group_invite(monkeypatch) -> None:
    """iPad 874 协议实现通用群邀请接口，不由插件直接构造协议请求。"""

    from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter
    from app.services.wechat_ipad.client import WechatIpadApiClient

    calls: list[dict[str, object]] = []

    def fake_invite(self, channel, *, wxid, chatroom_id, to_wxids):
        calls.append(
            {
                "channel_id": channel.id,
                "wxid": wxid,
                "chatroom_id": chatroom_id,
                "to_wxids": to_wxids,
            }
        )
        return {"Ret": 0, "Msg": "success"}

    monkeypatch.setattr(WechatIpadApiClient, "invite_chatroom_member", fake_invite)
    channel = Channel(
        id=88,
        bot_id=1,
        name="iPad",
        channel_type="wechat_ipad",
        api_base="http://protocol.example",
        adapter_config_json='{"wxid":"wxid-bot"}',
    )
    result = WechatIpadAdapter().invite_group_members(
        channel,
        chatroom_external_id="room@chatroom",
        member_external_ids=["wxid-user"],
    )

    assert result["ok"] is True
    assert result["action"] == "invite_group_members"
    assert calls == [
        {
            "channel_id": 88,
            "wxid": "wxid-bot",
            "chatroom_id": "room@chatroom",
            "to_wxids": ["wxid-user"],
        }
    ]


def test_group_auto_invite_uses_current_commands_and_trusted_user(group_plugin_fixture, monkeypatch) -> None:
    """自动拉群使用当前中文命令、内部群映射和通用邀请接口。"""

    factory, identity = group_plugin_fixture
    discovered = get_discovered_plugin("group_auto_invite", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("加群配置", "加群")
    assert discovered.capability.channel_types == ("wechat_ipad",)
    assert discovered.runtime_config == {
        "admin_user_ids": [],
        "require_direct_chat": True,
        "response_prefix": "",
    }

    loaded = load_directory_plugin("group_auto_invite", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    admin_context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
        channel_id=identity["invite_channel_id"],
        conversation_id=identity["invite_direct_conversation_id"],
        sender_id="admin-id",
        channel_type="wechat_ipad",
        chat_type="direct",
        message_type="text",
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        configured = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config={"admin_user_ids": ["admin-id"]},
                payload={
                    "command_name": "加群配置",
                    "command_args": "添加 产品群 产品交流群",
                    "channel_id": 99999,
                },
                context=admin_context,
            ),
        )
    assert configured.ok is True
    assert configured.output["config_patch"] == {
        "mappings": {"产品群": identity["invite_chatroom_id"]}
    }

    calls: list[dict[str, object]] = []

    def fake_invite(db, *, context, chatroom_id, member_external_ids):
        calls.append(
            {
                "context": context,
                "chatroom_id": chatroom_id,
                "member_external_ids": member_external_ids,
            }
        )
        return {"ok": True, "status": "sent"}

    monkeypatch.setattr(module, "invite_group_members", fake_invite)
    user_context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
        channel_id=identity["invite_channel_id"],
        conversation_id=identity["invite_direct_conversation_id"],
        sender_id="wxid-current-user",
        channel_type="wechat_ipad",
        chat_type="direct",
        message_type="text",
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        invited = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config={
                    "admin_user_ids": ["admin-id"],
                    "mappings": {"产品群": identity["invite_chatroom_id"]},
                },
                payload={
                    "command_name": "加群",
                    "command_args": "产品群",
                    "sender_id": "forged-user",
                    "channel_id": 99999,
                },
                context=user_context,
            ),
        )
    assert invited.ok is True
    assert invited.output["provider_status"] == "invite_sent"
    assert invited.output["replies"] == [{"type": "text", "content": "群邀请已发送，请在客户端确认。"}]
    assert calls[0]["chatroom_id"] == identity["invite_chatroom_id"]
    assert calls[0]["member_external_ids"] == ["wxid-current-user"]

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={},
            payload={"sender_id": "admin-id", "channel_id": identity["invite_channel_id"]},
        )
    )
    assert untrusted.ok is False
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/group_auto_invite/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "WechatIpadApiClient" not in source
    assert 'payload.get("sender_id")' not in source
    assert 'payload.get("channel_id")' not in source


def test_group_virtual_uses_trusted_groups_and_outbound_queue(group_plugin_fixture) -> None:
    """群桥保存平台群 ID，并通过统一队列完成跨渠道文本转发。"""

    factory, identity = group_plugin_fixture
    discovered = get_discovered_plugin("group_virtual", force=True)
    assert discovered is not None
    assert discovered.manifest.plugin_type == "observer"
    assert "telegram" in discovered.capability.channel_types
    assert discovered.matcher.commands == (
        "创建跨群",
        "添加群",
        "查看跨群列表",
        "查看跨群",
        "退出跨群",
        "解散跨群",
    )
    assert "bridges" not in discovered.runtime_config

    loaded = load_directory_plugin("group_virtual", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        management_message = Message(
            tenant_id=identity["tenant_id"],
            conversation_id=identity["primary_conversation_id"],
            direction="in",
            sender_id="bridge-admin",
            sender_name="桥管理员",
            content="#创建跨群 联盟",
            message_type="text",
            status="received",
        )
        db.add(management_message)
        db.flush()
        admin_context = PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["primary_conversation_id"],
            message_id=int(management_message.id),
            sender_id="bridge-admin",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        )
        created = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config={"admin_user_ids": ["bridge-admin"]},
                payload={
                    "command_name": "创建跨群",
                    "command_args": "联盟",
                    "conversation_external_id": "group-forged",
                    "channel_id": 99999,
                },
                context=admin_context,
            ),
        )

        source_message = Message(
            tenant_id=identity["tenant_id"],
            conversation_id=identity["primary_conversation_id"],
            direction="in",
            sender_id="member-source",
            sender_name="张三",
            content="跨渠道你好",
            message_type="text",
            status="received",
        )
        db.add(source_message)
        db.flush()
        message_context = PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["primary_conversation_id"],
            message_id=int(source_message.id),
            sender_id="member-source",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        )
        forwarded = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config={
                    "admin_user_ids": ["bridge-admin"],
                    "bridges": {
                        "联盟": [identity["primary_chatroom_id"], identity["invite_chatroom_id"]]
                    },
                },
                payload={
                    "content": "伪造正文",
                    "conversation_external_id": "group-forged",
                    "sender_name": "伪造昵称",
                },
                context=message_context,
            ),
        )
        jobs = db.scalars(select(TaskJob).order_by(TaskJob.id.asc())).all()

    assert created.ok is True
    assert created.output["config_patch"] == {
        "bridges": {"联盟": [identity["primary_chatroom_id"]]}
    }
    assert forwarded.ok is True
    assert forwarded.output["provider_status"] == "forward_queued"
    assert forwarded.output["target_count"] == 1
    assert len(jobs) == 1
    job_payload = json.loads(jobs[0].payload_json)
    assert job_payload["conversation_id"] == identity["invite_group_conversation_id"]
    assert job_payload["payload"]["content"] == "[群桥] 张三 来自 主测试群：跨渠道你好"
    assert job_payload["payload"]["receiver_id"] == "invite-room@chatroom"

    source = (PROJECT_ROOT / "plugins/group_virtual/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "WechatIpadApiClient" not in source
    assert 'payload.get("channel_id")' not in source
    assert 'payload.get("conversation_external_id")' not in source
    assert 'payload.get("sender_id")' not in source


def test_guess_song_uses_tenant_music_service_and_state_store(group_plugin_fixture, monkeypatch) -> None:
    """猜歌名只使用租户音乐配置，并把答案状态绑定到可信群会话。"""

    from app.services.plugin_runtime.state_store import PluginStateStore

    factory, identity = group_plugin_fixture
    discovered = get_discovered_plugin("guess_song", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("猜歌名",)
    assert "telegram" in discovered.capability.channel_types
    assert discovered.secret_fields == ("api_key",)
    assert discovered.runtime_config["api_base"] == ""
    assert "game_sessions" not in discovered.runtime_config
    assert "192.168.1.240" not in str(discovered.runtime_config)

    loaded = load_directory_plugin("guess_song", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(
        module,
        "_search_random_song",
        lambda config, *, singer_name: module.SongResult(
            song_id="song-001",
            song_name="稻香",
            artist_name="周杰伦",
            play_url="https://media.example.test/song-001.mp3",
            cover_url="https://media.example.test/song-001.jpg",
        ),
    )
    context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
        channel_id=identity["channel_id"],
        conversation_id=identity["primary_conversation_id"],
        sender_id="guess-user",
        channel_type="telegram",
        chat_type="group",
        message_type="text",
    )
    config = {
        "api_base": "https://music.example.test",
        "api_key": "tenant-key",
        "quality": "exhigh",
        "hint_seconds": 30,
        "game_ttl_seconds": 1800,
    }
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        started = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config=config,
                payload={
                    "command_name": "猜歌名",
                    "command_args": "周杰伦",
                    "conversation_id": identity["forged_conversation_id"],
                },
                context=context,
            ),
        )
        store = PluginStateStore(db, context)
        state_key = f"conversation:{identity['primary_conversation_id']}"
        state = store.get("guess_song", state_key)
        assert state is not None
        state["started_at"] = int(now_shanghai().timestamp()) - 60
        store.put("guess_song", state_key, state, ttl_seconds=1800)

        wrong = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config=config,
                payload={"command_name": "猜歌名", "command_args": "青花瓷"},
                context=context,
            ),
        )
        correct = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config=config,
                payload={"command_name": "猜歌名", "command_args": "稻香"},
                context=context,
            ),
        )
        remaining_state = store.get("guess_song", state_key)

    assert started.ok is True
    assert started.output["provider_status"] == "started"
    assert started.output["replies"][0] == {
        "type": "file",
        "file_name": "guess-song-preview.mp3",
        "file_url": "https://media.example.test/song-001.mp3",
    }
    assert "song_name" not in started.output
    assert wrong.output["provider_status"] == "incorrect_with_hint"
    assert "歌手是 周杰伦" in wrong.output["replies"][0]["content"]
    assert "稻香" not in wrong.output["replies"][0]["content"]
    assert correct.output["provider_status"] == "correct"
    assert correct.output["replies"][1]["file_name"] == "周杰伦 - 稻香.mp3"
    assert remaining_state is None

    untrusted = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config=config,
            payload={"conversation_id": identity["primary_conversation_id"]},
        )
    )
    assert untrusted.ok is False
    assert untrusted.output["provider_status"] == "trusted_context_required"

    source = (PROJECT_ROOT / "plugins/guess_song/plugin.py").read_text(encoding="utf-8")
    assert "192.168.1.240" not in source
    assert "urlrequest" not in source
    assert "urlopen" not in source
    assert "game_sessions" not in source
    assert "config_patch" not in source
    assert "<appmsg" not in source
    assert "SessionLocal" not in source
    assert 'payload.get("conversation_id")' not in source
