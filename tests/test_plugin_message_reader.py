from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-message-reader"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import SHANGHAI_TZ
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Message, Plugin
from app.models.saas import Tenant
from app.services.plugin_runtime.contracts import PluginExecutionContext
from app.services.plugin_runtime.messages import PluginMessageAccessError, PluginMessageReader


@pytest.fixture()
def message_reader_fixture(tmp_path: Path):
    """建立跨租户、跨机器人和私聊数据，集中验证消息读取边界。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'plugin-message-reader.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    base_time = datetime(2026, 8, 30, 0, 0, tzinfo=SHANGHAI_TZ)
    with platform_session(factory) as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        plugin = Plugin(code="message_summary", display_name="群聊总结", plugin_type="command")
        db.add_all([alpha, beta, plugin])
        db.flush()

        alpha_bot = Bot(tenant_id=alpha.id, name="甲机器人", status="running", is_enabled=True)
        alpha_other_bot = Bot(tenant_id=alpha.id, name="甲备用机器人", status="running", is_enabled=True)
        beta_bot = Bot(tenant_id=beta.id, name="乙机器人", status="running", is_enabled=True)
        db.add_all([alpha_bot, alpha_other_bot, beta_bot])
        db.flush()

        alpha_channel = Channel(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            name="甲主渠道",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        other_channel = Channel(
            tenant_id=alpha.id,
            bot_id=alpha_other_bot.id,
            name="甲备用渠道",
            channel_type="wechat_ipad",
            status="online",
            is_enabled=True,
        )
        beta_channel = Channel(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            name="乙主渠道",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add_all([alpha_channel, other_channel, beta_channel])
        db.flush()

        group = Conversation(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            channel_id=alpha_channel.id,
            channel_type="telegram",
            external_id="alpha-group",
            title="产品讨论群",
            chat_type="group",
        )
        direct = Conversation(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            channel_id=alpha_channel.id,
            channel_type="telegram",
            external_id="alpha-direct",
            title="客户私聊",
            chat_type="direct",
        )
        other_bot_group = Conversation(
            tenant_id=alpha.id,
            bot_id=alpha_other_bot.id,
            channel_id=other_channel.id,
            channel_type="wechat_ipad",
            external_id="alpha-other-group",
            title="备用群",
            chat_type="group",
        )
        beta_group = Conversation(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            channel_id=beta_channel.id,
            channel_type="telegram",
            external_id="beta-group",
            title="乙租户群",
            chat_type="group",
        )
        binding = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add_all([group, direct, other_bot_group, beta_group, binding])
        db.flush()

        # 两条消息故意使用相同时间，测试结果仍按数据库主键稳定排序。
        db.add_all(
            [
                Message(
                    tenant_id=alpha.id,
                    conversation_id=group.id,
                    direction="in",
                    sender_id="member-1",
                    sender_name="张三",
                    content="范围外消息",
                    message_type="text",
                    status="received",
                    created_at=base_time - timedelta(seconds=1),
                ),
                Message(
                    tenant_id=alpha.id,
                    conversation_id=group.id,
                    direction="in",
                    sender_id="member-1",
                    sender_name="张三",
                    content="第一条",
                    message_type="text",
                    status="received",
                    created_at=base_time + timedelta(hours=1),
                ),
                Message(
                    tenant_id=alpha.id,
                    conversation_id=group.id,
                    direction="out",
                    content="第二条",
                    message_type="text",
                    status="sent",
                    created_at=base_time + timedelta(hours=1),
                ),
                Message(
                    tenant_id=alpha.id,
                    conversation_id=group.id,
                    direction="in",
                    sender_name="李四",
                    content="图片占位",
                    message_type="image",
                    status="received",
                    created_at=base_time + timedelta(hours=2),
                ),
                Message(
                    tenant_id=alpha.id,
                    conversation_id=group.id,
                    direction="in",
                    sender_name="王五",
                    content="   ",
                    message_type="text",
                    status="received",
                    created_at=base_time + timedelta(hours=3),
                ),
            ]
        )
        db.commit()
        identity = {
            "tenant_id": int(alpha.id),
            "other_tenant_id": int(beta.id),
            "bot_id": int(alpha_bot.id),
            "channel_id": int(alpha_channel.id),
            "binding_id": int(binding.id),
            "group_id": int(group.id),
            "direct_id": int(direct.id),
            "other_bot_group_id": int(other_bot_group.id),
            "beta_group_id": int(beta_group.id),
        }
    try:
        yield factory, identity, base_time
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _context(identity: dict[str, int], **overrides: int | str | None) -> PluginExecutionContext:
    values: dict[str, int | str | None] = {
        "tenant_id": identity["tenant_id"],
        "binding_id": identity["binding_id"],
        "bot_id": identity["bot_id"],
        "channel_id": identity["channel_id"],
        "conversation_id": identity["group_id"],
        "channel_type": "telegram",
        "chat_type": "group",
    }
    values.update(overrides)
    return PluginExecutionContext(**values)  # type: ignore[arg-type]


def test_reader_returns_only_text_messages_in_requested_window(message_reader_fixture) -> None:
    factory, identity, base_time = message_reader_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        batch = PluginMessageReader(db, _context(identity)).read(
            start_at=base_time,
            end_at=base_time + timedelta(days=1),
            limit=20,
        )

    assert batch.conversation_id == identity["group_id"]
    assert batch.conversation_title == "产品讨论群"
    assert batch.total == 2
    assert [item.content for item in batch.messages] == ["第一条", "第二条"]
    assert [item.formatted_text for item in batch.messages] == ["张三: 第一条", "机器人: 第二条"]
    assert batch.messages[0].message_id < batch.messages[1].message_id


@pytest.mark.parametrize(
    ("context_overrides", "error_text"),
    [
        ({"tenant_id": 999}, "租户"),
        ({"conversation_id": None}, "会话"),
        ({"conversation_id": "direct"}, "群聊"),
        ({"conversation_id": "other_bot"}, "机器人"),
        ({"channel_id": 999}, "渠道"),
    ],
)
def test_reader_rejects_untrusted_or_mismatched_context(
    message_reader_fixture,
    context_overrides: dict[str, object],
    error_text: str,
) -> None:
    factory, identity, base_time = message_reader_fixture
    resolved = dict(context_overrides)
    if resolved.get("conversation_id") == "direct":
        resolved["conversation_id"] = identity["direct_id"]
    elif resolved.get("conversation_id") == "other_bot":
        resolved["conversation_id"] = identity["other_bot_group_id"]

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        with pytest.raises(PluginMessageAccessError, match=error_text):
            PluginMessageReader(db, _context(identity, **resolved)).read(
                start_at=base_time,
                end_at=base_time + timedelta(days=1),
                limit=20,
            )


def test_reader_rejects_invalid_range_and_excessive_limit(message_reader_fixture) -> None:
    factory, identity, base_time = message_reader_fixture
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        reader = PluginMessageReader(db, _context(identity))
        with pytest.raises(PluginMessageAccessError, match="时间范围"):
            reader.read(start_at=base_time, end_at=base_time, limit=20)
        with pytest.raises(PluginMessageAccessError, match="2000"):
            reader.read(start_at=base_time, end_at=base_time + timedelta(days=1), limit=2001)
