from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-member-roast"
SHANGHAI_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 9, 1, 14, 0, tzinfo=SHANGHAI_TZ)

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
    ChatroomMember,
    Conversation,
    Message,
    Plugin,
    PluginStateRecord,
)
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.text_generation import PluginTextGenerationResult


class FakeTextGenerationClient:
    """记录压缩提示词，并返回一段符合业务格式的锐评。"""

    def __init__(self):
        self.init_kwargs: dict[str, object] = {}
        self.calls: list[dict[str, object]] = []

    def generate(self, **kwargs) -> PluginTextGenerationResult:
        self.calls.append(dict(kwargs))
        return PluginTextGenerationResult(
            text="【锐评 乙成员】\n做事先看证据，聊天先问日志。\n嘴上说简单一点，实际需求总能再长一截。\n一句话绝杀：不是在推进，就是在催别人推进。",
            token_usage=321,
        )


def _profile_state_key(channel_id: int, chatroom_id: int, member_id: str) -> str:
    """按成员画像插件当前稳定规则生成测试状态键。"""

    digest = hashlib.sha256(member_id.encode("utf-8")).hexdigest()[:24]
    return f"channel-{channel_id}:chatroom-{chatroom_id}:member-{digest}"


@pytest.fixture()
def member_roast_fixture(tmp_path: Path):
    """准备同一机器人下的画像绑定、锐评绑定、群成员和有限历史消息。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'member-roast.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="member-roast", name="锐评租户", status="active")
        context_plugin = Plugin(code="member_context", display_name="群成员画像", plugin_type="scheduled")
        roast_plugin = Plugin(code="member_roast", display_name="群成员锐评", plugin_type="command")
        db.add_all([tenant, context_plugin, roast_plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="锐评机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="锐评 Telegram",
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
            external_id="roast-room",
            title="产品研发群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            external_id=conversation.external_id,
            name=conversation.title,
            status="active",
            is_active=True,
            member_count=2,
        )
        context_binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=context_plugin.id,
            status="enabled",
            config_json="{}",
        )
        roast_binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=roast_plugin.id,
            status="enabled",
            config_json="{}",
        )
        db.add_all([chatroom, context_binding, roast_binding])
        db.flush()
        db.add_all(
            [
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="user-a",
                    display_name="甲成员",
                    status="active",
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="user-b",
                    display_name="乙成员",
                    status="active",
                ),
            ]
        )
        for index in range(12):
            db.add(
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="in",
                    sender_id="user-b",
                    sender_name="乙成员",
                    content=f"乙成员历史发言{index}：先看日志和接口，再推进第{index}项研发。",
                    created_at=FIXED_NOW - timedelta(days=1, minutes=30 - index),
                )
            )
        for index in range(8):
            db.add(
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="in",
                    sender_id="user-a",
                    sender_name="甲成员",
                    content=f"甲成员历史发言{index}：这个需求能否先确认再开发？",
                    created_at=FIXED_NOW - timedelta(days=1, minutes=15 - index),
                )
            )
        command_message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction="in",
            sender_id="user-a",
            sender_name="甲成员",
            content="锐评一下 @乙成员",
            created_at=FIXED_NOW,
        )
        db.add(command_message)
        db.flush()

        for member_id, display_name in (("user-a", "甲成员"), ("user-b", "乙成员")):
            db.add(
                PluginStateRecord(
                    tenant_id=tenant.id,
                    bot_plugin_id=context_binding.id,
                    namespace="member_context_profiles",
                    state_key=_profile_state_key(channel.id, chatroom.id, member_id),
                    payload_json=json.dumps(
                        {
                            "member_id": member_id,
                            "display_name": display_name,
                            "topics_of_interest": ["产品研发", "接口验证"],
                            "recent_focus": ["交付进度"],
                            "summary_text": "习惯先验证再推动研发，表达直接。",
                            "activity_level": "稳定活跃",
                            "message_pattern": "偏简洁",
                            "interaction_style": "先确认信息再推进",
                            "response_style_hint": "先给结论再给依据",
                            "last_message_id": 1,
                            "meta": {
                                "skill_profile": ["拆解问题"],
                                "problem_solving_profile": ["先验证再推进"],
                                "expression_profile": ["先说结论"],
                                "group_role": "推进者",
                            },
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "chatroom_id": int(chatroom.id),
            "roast_binding_id": int(roast_binding.id),
            "context_binding_id": int(context_binding.id),
            "message_id": int(command_message.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(identity: dict[str, int], *, payload: dict[str, object] | None = None) -> PluginExecutionRequest:
    """构造只能由平台消息运行时生成的可信锐评请求。"""

    return PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "llm_api_base": "https://llm.example.test/v1",
            "llm_api_key": "tenant-roast-key",
            "llm_model": "tenant-chat-model",
            "llm_timeout_seconds": 60,
            "sample_days": 30,
            "message_limit": 20,
            "min_message_count": 3,
            "representative_message_limit": 4,
            "user_rate_limit": 3,
            "group_rate_limit": 10,
            "rate_window_seconds": 60,
            "max_output_chars": 320,
        },
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["roast_binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=identity["message_id"],
            sender_id="user-a",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def test_member_roast_manifest_uses_tenant_llm_secret() -> None:
    """锐评是群命令插件，模型密钥必须从普通配置中剥离。"""

    discovered = get_discovered_plugin("member_roast", force=True)

    assert discovered is not None
    assert discovered.is_valid is True
    assert discovered.manifest.plugin_type == "command"
    assert "锐评一下" in discovered.matcher.commands
    assert "锐评我" in discovered.matcher.commands
    assert discovered.secret_fields == ("llm_api_key",)
    assert "llm_api_key" not in discovered.runtime_config
    assert discovered.runtime_config["llm_model"] == ""


def test_roast_resolves_group_member_reuses_profile_and_compresses_history(
    member_roast_fixture,
    monkeypatch,
) -> None:
    """目标来自真实消息和当前群成员，模型只收到画像、统计和有限代表句。"""

    factory, identity = member_roast_fixture
    loaded = load_directory_plugin("member_roast", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake = FakeTextGenerationClient()

    def fake_factory(**kwargs):
        fake.init_kwargs = dict(kwargs)
        return fake

    monkeypatch.setattr(module, "PluginTextGenerationClient", fake_factory)
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = loaded.instance.execute_message_action(
            db,
            _request(identity, payload={"target_user_id": "outside-user"}),
        )

        assert result.ok is True
        assert result.output["provider_status"] == "completed"
        assert result.output["target_member_id"] == "user-b"
        assert result.output["target_display_name"] == "乙成员"
        assert result.output["sample_message_count"] == 12
        assert result.output["token_usage"] == 321
        assert result.output["replies"][0]["content"].startswith("【锐评 乙成员】")
        assert fake.init_kwargs == {
            "api_base": "https://llm.example.test/v1",
            "model": "tenant-chat-model",
            "api_key": "tenant-roast-key",
        }
        assert len(fake.calls) == 1
        user_prompt = str(fake.calls[0]["user_prompt"])
        assert "outside-user" not in user_prompt
        assert "产品研发" in user_prompt
        assert "乙成员历史发言0" not in user_prompt
        prompt_payload = json.loads(user_prompt)
        assert len(prompt_payload["recent_message_profile"]["representative_messages"]) == 4


def test_roast_self_target_and_missing_profile_are_explicit(
    member_roast_fixture,
    monkeypatch,
) -> None:
    """“锐评我”定位真实发送人；画像不存在时不临时复制画像插件逻辑。"""

    factory, identity = member_roast_fixture
    loaded = load_directory_plugin("member_roast", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake = FakeTextGenerationClient()
    monkeypatch.setattr(module, "PluginTextGenerationClient", lambda **_kwargs: fake)
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        command = db.get(Message, identity["message_id"])
        assert command is not None
        command.content = "锐评我"
        result = loaded.instance.execute_message_action(db, _request(identity))
        assert result.ok is True
        assert result.output["target_member_id"] == "user-a"

        profile = db.get(PluginStateRecord, 1)
        profiles = db.query(PluginStateRecord).filter(
            PluginStateRecord.namespace == "member_context_profiles",
            PluginStateRecord.bot_plugin_id == identity["context_binding_id"],
        ).all()
        for item in profiles:
            if json.loads(item.payload_json)["member_id"] == "user-a":
                profile = item
                break
        assert profile is not None
        db.delete(profile)
        db.flush()

        missing = loaded.instance.execute_message_action(db, _request(identity))
        assert missing.ok is False
        assert missing.output["provider_status"] == "profile_not_ready"
        assert len(fake.calls) == 1
