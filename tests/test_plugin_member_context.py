from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-member-context"
SHANGHAI_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI_TZ)

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
from app.services.plugin_runtime.http_client import PluginHttpResponse


class FakeDifyClient:
    """返回一个候选成员和一个越权成员，验证结果白名单与密钥边界。"""

    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def post_json(self, url: str, payload: object, **kwargs) -> PluginHttpResponse:
        self.calls.append({"url": url, "payload": payload, "kwargs": kwargs})
        output = {
            "members": [
                {
                    "member_id": "user-a",
                    "topics": ["产品研发", "自动化"],
                    "skill_profile": ["善于拆解问题"],
                    "problem_solving_profile": ["先验证再推进"],
                    "value_profile": ["效率优先"],
                    "expression_profile": ["先说结论"],
                    "reply_entry_profile": ["可从实现细节切入"],
                    "group_role": "推进者",
                    "decision_profile": "偏向基于证据快速决策",
                    "summary_text": "持续关注产品研发和自动化，习惯先验证再推进。",
                    "confidence": 0.88,
                },
                {
                    "member_id": "outside-user",
                    "topics": ["不应写入"],
                    "summary_text": "越权结果",
                },
            ]
        }
        body = {"data": {"outputs": {"text": json.dumps(output, ensure_ascii=False)}}}
        return PluginHttpResponse(
            status_code=200,
            headers={"content-type": "application/json; charset=utf-8"},
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            url=url,
        )


@pytest.fixture()
def member_context_fixture(tmp_path: Path):
    """建立一个群、两个成员和可形成画像的入站文本消息。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'member-context.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="member-context", name="画像租户", status="active")
        plugin = Plugin(code="member_context", display_name="群成员画像", plugin_type="scheduled")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="画像机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="画像 Telegram",
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
            external_id="profile-room",
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
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json=json.dumps(
                {"member_context_conversation_ids": [conversation.id]},
                ensure_ascii=False,
            ),
        )
        db.add_all([chatroom, binding])
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
        message_time = FIXED_NOW - timedelta(days=1)
        db.add_all(
            [
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="in",
                    sender_id="user-a",
                    sender_name="甲成员",
                    content="先确认接口返回，再推进自动化研发。",
                    created_at=message_time.replace(hour=9),
                ),
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="in",
                    sender_id="user-a",
                    sender_name="甲成员",
                    content="产品流程需要更清晰，开发优先处理主线。",
                    created_at=message_time.replace(hour=10),
                ),
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="in",
                    sender_id="user-b",
                    sender_name="乙成员",
                    content="这个问题怎么验证？先看日志可以吗？",
                    created_at=message_time.replace(hour=11),
                ),
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="in",
                    sender_id="user-b",
                    sender_name="乙成员",
                    content="我整理测试结果，再同步大家。",
                    created_at=message_time.replace(hour=12),
                ),
                # 出站消息不能进入成员画像样本。
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="out",
                    sender_id="user-a",
                    sender_name="甲成员",
                    content="机器人回复",
                    created_at=message_time.replace(hour=13),
                ),
            ]
        )
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "binding_id": int(binding.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "chatroom_id": int(chatroom.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(
    identity: dict[str, int],
    *,
    action: str = "member_context_daily_refresh",
    api_base: str = "",
    api_key: str = "",
) -> PluginExecutionRequest:
    """构造统一调度器传入的可信群会话上下文。"""

    return PluginExecutionRequest(
        action=action,
        config={
            "enabled": True,
            "api_base": api_base,
            "api_key": api_key,
            "request_timeout_seconds": 60,
            "sample_days": 30,
            "incremental_message_limit": 80,
            "minimum_member_messages": 2,
            "ai_min_member_messages": 2,
            "max_members_per_run": 10,
            "max_group_messages": 200,
        },
        payload={},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            channel_type="telegram",
            chat_type="group",
        ),
    )


def test_member_context_manifest_is_scheduled_and_uses_tenant_llm_secret() -> None:
    """成员画像没有聊天命令，Dify 密钥必须从普通运行配置中剥离。"""

    discovered = get_discovered_plugin("member_context", force=True)

    assert discovered is not None
    assert discovered.is_valid is True
    assert discovered.manifest.plugin_type == "scheduled"
    assert discovered.matcher.commands == ()
    assert discovered.secret_fields == ("api_key",)
    assert "api_key" not in discovered.runtime_config
    assert discovered.runtime_config["member_context_conversation_ids"] == []
    assert discovered.runtime_config["daily_refresh_time"] == "04:20"


def test_daily_refresh_builds_local_profiles_and_only_merges_new_messages(
    member_context_fixture,
    monkeypatch,
) -> None:
    """无 Dify 配置时生成本地画像，重复刷新跳过，新增消息后只推进水位。"""

    factory, identity = member_context_fixture
    loaded = load_directory_plugin("member_context", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = loaded.instance.execute_scheduled_action(db, _request(identity))
        db.commit()
        records = db.scalars(
            select(PluginStateRecord).where(PluginStateRecord.namespace == "member_context_profiles")
        ).all()
        first_payloads = [json.loads(item.payload_json) for item in records]
        first_watermarks = {item["member_id"]: int(item["last_message_id"]) for item in first_payloads}

        assert first.ok is True
        assert first.output["refreshed_member_count"] == 2
        assert first.output["provider_status"] == "local"
        assert len(records) == 2
        assert all(
            item.state_key.startswith(
                f"channel-{identity['channel_id']}:chatroom-{identity['chatroom_id']}:member-"
            )
            for item in records
        )
        assert {item["member_id"] for item in first_payloads} == {"user-a", "user-b"}
        assert all(item["source_message_count"] == 2 for item in first_payloads)
        assert all(item["topics_of_interest"] for item in first_payloads)

        second = loaded.instance.execute_scheduled_action(db, _request(identity))
        assert second.ok is True
        assert second.status == "skipped"
        assert second.output["refreshed_member_count"] == 0

        new_message = Message(
            tenant_id=identity["tenant_id"],
            conversation_id=identity["conversation_id"],
            direction="in",
            sender_id="user-a",
            sender_name="甲成员",
            content="新增消息只用于增量更新画像。",
            created_at=FIXED_NOW - timedelta(hours=1),
        )
        db.add(new_message)
        db.commit()
        third = loaded.instance.execute_scheduled_action(db, _request(identity))
        db.commit()
        updated = [
            json.loads(item.payload_json)
            for item in db.scalars(
                select(PluginStateRecord).where(PluginStateRecord.namespace == "member_context_profiles")
            ).all()
        ]
        updated_map = {item["member_id"]: item for item in updated}

        assert third.ok is True
        assert third.output["refreshed_member_count"] == 1
        assert updated_map["user-a"]["last_message_id"] > first_watermarks["user-a"]
        assert updated_map["user-a"]["source_message_count_total"] == 3
        assert updated_map["user-b"]["last_message_id"] == first_watermarks["user-b"]


def test_dify_batch_result_only_updates_current_group_candidates(
    member_context_fixture,
    monkeypatch,
) -> None:
    """Dify 批量结果只能覆盖当前候选成员，密钥和完整请求不得写入画像状态。"""

    factory, identity = member_context_fixture
    loaded = load_directory_plugin("member_context", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_client = FakeDifyClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_client)
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = loaded.instance.execute_scheduled_action(
            db,
            _request(
                identity,
                api_base="https://dify.example.test/v1",
                api_key="tenant-profile-key",
            ),
        )
        db.commit()
        payloads = [
            json.loads(item.payload_json)
            for item in db.scalars(
                select(PluginStateRecord).where(PluginStateRecord.namespace == "member_context_profiles")
            ).all()
        ]
        profile_map = {item["member_id"]: item for item in payloads}

        assert result.ok is True
        assert result.output["provider_status"] == "completed"
        assert result.output["ai_profile_count"] == 1
        assert len(fake_client.calls) == 1
        call = fake_client.calls[0]
        assert call["url"] == "https://dify.example.test/v1/workflows/run"
        assert call["kwargs"]["headers"]["Authorization"] == "Bearer tenant-profile-key"
        assert set(profile_map) == {"user-a", "user-b"}
        assert profile_map["user-a"]["summary_text"] == "持续关注产品研发和自动化，习惯先验证再推进。"
        assert profile_map["user-a"]["meta"]["group_role"] == "推进者"
        serialized = json.dumps(payloads, ensure_ascii=False)
        assert "outside-user" not in serialized
        assert "tenant-profile-key" not in serialized
        assert "compressed_chat" not in serialized
