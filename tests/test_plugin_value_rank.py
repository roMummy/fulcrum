from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-value-rank"
SHANGHAI_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 9, 1, 15, 0, tzinfo=SHANGHAI_TZ)

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
    PointsAccount,
)
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest


@pytest.fixture()
def value_rank_fixture(tmp_path: Path):
    """准备三位群成员、积分、发言和可形成社交边的标准文本。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'value-rank.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="value-rank", name="身价租户", status="active")
        plugin = Plugin(code="value_rank", display_name="群成员身价", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="身价机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="身价 Telegram",
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
            external_id="value-room",
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
            member_count=3,
        )
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json="{}",
        )
        db.add_all([chatroom, binding])
        db.flush()
        members = [
            ("user-a", "甲成员"),
            ("user-b", "乙成员"),
            ("user-c", "丙成员"),
        ]
        for member_id, display_name in members:
            db.add(
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id=member_id,
                    display_name=display_name,
                    status="active",
                )
            )
        db.add_all(
            [
                PointsAccount(
                    tenant_id=tenant.id,
                    user_id="user-a",
                    group_id=conversation.external_id,
                    user_name="甲成员",
                    total_points=120,
                ),
                PointsAccount(
                    tenant_id=tenant.id,
                    user_id="user-b",
                    group_id=conversation.external_id,
                    user_name="乙成员",
                    total_points=80,
                ),
                PointsAccount(
                    tenant_id=tenant.id,
                    user_id="user-c",
                    group_id=conversation.external_id,
                    user_name="丙成员",
                    total_points=20,
                ),
            ]
        )
        historical_messages = [
            ("user-a", "甲成员", "@乙成员 一起确认接口。", 1),
            ("user-b", "乙成员", "@甲成员 日志已经整理。", 2),
            ("user-a", "甲成员", "@丙成员 帮忙看一下数据。", 3),
            ("user-c", "丙成员", "收到，我来处理。", 4),
            ("user-a", "甲成员", "今天继续推进研发。", 5),
            ("user-b", "乙成员", "测试结果已经同步。", 6),
            ("user-a", "甲成员", "@乙成员 再确认一次。", 7),
        ]
        for sender_id, sender_name, content, hour in historical_messages:
            db.add(
                Message(
                    tenant_id=tenant.id,
                    conversation_id=conversation.id,
                    direction="in",
                    sender_id=sender_id,
                    sender_name=sender_name,
                    content=content,
                    created_at=FIXED_NOW - timedelta(days=1, hours=hour),
                )
            )
        command_message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction="in",
            sender_id="user-a",
            sender_name="甲成员",
            content="我的身价",
            created_at=FIXED_NOW,
        )
        db.add(command_message)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "binding_id": int(binding.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "chatroom_id": int(chatroom.id),
            "message_id": int(command_message.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(
    identity: dict[str, int],
    *,
    administrators: list[str] | None = None,
    payload: dict[str, object] | None = None,
) -> PluginExecutionRequest:
    """构造真实群消息运行时请求，业务目标和管理员身份不从载荷读取。"""

    return PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "message_window_days": 7,
            "active_window_days": 30,
            "social_window_days": 7,
            "points_weight": 0.30,
            "message_weight": 0.35,
            "active_days_weight": 0.20,
            "social_weight": 0.15,
            "inactivity_penalty_max": 150,
            "base_score_scale": 1000,
            "default_rank_limit": 10,
            "max_rank_limit": 50,
            "default_trend_days": 7,
            "max_trend_days": 30,
            "administrator_user_ids": administrators or [],
        },
        payload=payload or {},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
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


def test_value_rank_manifest_is_channel_neutral() -> None:
    """身价插件声明完整命令集，但不依赖任何 Provider 配置或密钥。"""

    discovered = get_discovered_plugin("value_rank", force=True)

    assert discovered is not None
    assert discovered.is_valid is True
    assert discovered.manifest.plugin_type == "command"
    assert "我的身价" in discovered.matcher.commands
    assert "社交关系图" in discovered.matcher.commands
    assert "重算身价" in discovered.matcher.commands
    assert discovered.secret_fields == ()
    assert discovered.runtime_config["value_rank_conversation_ids"] == []


def test_my_value_recomputes_snapshot_from_current_group_data(
    value_rank_fixture,
    monkeypatch,
) -> None:
    """首次查询按当前群积分、消息、活跃和社交关系生成可解释快照。"""

    factory, identity = value_rank_fixture
    loaded = load_directory_plugin("value_rank", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = loaded.instance.execute_message_action(
            db,
            _request(identity, payload={"conversation_id": 9999, "user_id": "outside"}),
        )
        records = db.scalars(
            select(PluginStateRecord).where(PluginStateRecord.namespace == "value_rank_snapshots")
        ).all()

        assert result.ok is True
        assert result.output["provider_status"] == "completed"
        assert result.output["command"] == "我的身价"
        assert result.output["reply_text"].startswith("甲成员 的身价报告")
        assert result.output["snapshot_member_count"] == 3
        assert len(records) == 1
        snapshot = json.loads(records[0].payload_json)
        assert snapshot["conversation_id"] == identity["conversation_id"]
        assert {row["user_id"] for row in snapshot["rows"]} == {"user-a", "user-b", "user-c"}
        assert snapshot["partners"][0]["member_ids"] == ["user-a", "user-b"]
        assert "outside" not in records[0].payload_json


@pytest.mark.parametrize(
    ("command", "expected_text"),
    [
        ("身价排行 2", "身价排行榜"),
        ("社交热度榜 3", "社交热度榜"),
        ("搭子榜 3", "搭子榜"),
        ("社交桥梁榜 3", "社交桥梁榜"),
        ("社交关系图", "社交关系摘要"),
        ("身价周报", "身价周报"),
        ("身价说明", "身价算法说明"),
    ],
)
def test_value_rank_business_views_share_one_snapshot(
    value_rank_fixture,
    monkeypatch,
    command: str,
    expected_text: str,
) -> None:
    """排行、社交和周报视图共享同一结构化快照，不回扫 Provider 数据。"""

    factory, identity = value_rank_fixture
    loaded = load_directory_plugin("value_rank", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        message = db.get(Message, identity["message_id"])
        assert message is not None
        message.content = command
        result = loaded.instance.execute_message_action(db, _request(identity))

        assert result.ok is True
        assert expected_text in result.output["reply_text"]


def test_trend_and_admin_recompute_use_current_sender(
    value_rank_fixture,
    monkeypatch,
) -> None:
    """趋势只查询真实发送人，重算权限只接受租户配置管理员名单。"""

    factory, identity = value_rank_fixture
    loaded = load_directory_plugin("value_rank", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        message = db.get(Message, identity["message_id"])
        assert message is not None
        message.content = "我的身价"
        loaded.instance.execute_message_action(db, _request(identity))
        today_record = db.scalar(
            select(PluginStateRecord).where(PluginStateRecord.namespace == "value_rank_snapshots")
        )
        assert today_record is not None
        yesterday = json.loads(today_record.payload_json)
        yesterday["date_key"] = "2026-08-31"
        for row in yesterday["rows"]:
            row["score"] = max(0, float(row["score"]) - 20)
        db.add(
            PluginStateRecord(
                tenant_id=identity["tenant_id"],
                bot_plugin_id=identity["binding_id"],
                namespace="value_rank_snapshots",
                state_key=f"conversation-{identity['conversation_id']}:date-2026-08-31",
                payload_json=json.dumps(yesterday, ensure_ascii=False),
            )
        )
        db.flush()

        message.content = "我的趋势 7"
        trend = loaded.instance.execute_message_action(
            db,
            _request(identity, payload={"sender_id": "user-b"}),
        )
        assert trend.ok is True
        assert "甲成员 的身价趋势" in trend.output["reply_text"]
        assert "2026-08-31" in trend.output["reply_text"]

        message.content = "重算身价"
        denied = loaded.instance.execute_message_action(db, _request(identity))
        assert denied.ok is False
        assert denied.output["provider_status"] == "administrator_required"

        allowed = loaded.instance.execute_message_action(
            db,
            _request(identity, administrators=["user-a"]),
        )
        assert allowed.ok is True
        assert allowed.output["provider_status"] == "recomputed"
