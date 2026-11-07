from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-daily-ranking"
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
    PointsAccount,
    PointsTransaction,
    Plugin,
    PluginStateRecord,
    TaskJob,
)
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.task_queue import parse_job_payload


@pytest.fixture()
def daily_ranking_fixture(tmp_path: Path):
    """建立两个群聊及跨日期消息，验证日榜只统计可信目标群的昨日入站消息。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'daily-ranking.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="daily-ranking", name="日榜租户", status="active")
        plugin = Plugin(code="daily_ranking", display_name="群聊每日排行", plugin_type="scheduled")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="日榜机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="日榜 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        target = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="daily-room",
            title="产品交流群",
            chat_type="group",
        )
        other = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="telegram",
            external_id="other-room",
            title="其他群",
            chat_type="group",
        )
        db.add_all([target, other])
        db.flush()
        chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            external_id=target.external_id,
            name=target.title,
            status="active",
            is_active=True,
            member_count=3,
        )
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "enabled": True,
                    "daily_ranking_conversation_ids": [target.id],
                    "display_limit": 10,
                    "reward_limit": 20,
                    "render_as_image": True,
                },
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
                    member_external_id="alice",
                    nickname="Alice",
                    display_name="爱丽丝",
                    status="active",
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="bob",
                    nickname="小波",
                    display_name=None,
                    status="active",
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="inactive",
                    nickname="已离群",
                    display_name=None,
                    status="left",
                ),
            ]
        )

        # 昨日目标群的有效入站消息为 Alice 三条、Bob 两条。出站、其他群和今日消息
        # 都是有意加入的干扰数据，确保排行不会跨方向、跨群或跨自然日统计。
        yesterday = FIXED_NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        messages = [
            Message(
                tenant_id=tenant.id,
                conversation_id=target.id,
                direction="in",
                sender_id="alice",
                sender_name="旧名称 Alice",
                content=f"Alice 消息 {index}",
                created_at=yesterday + timedelta(hours=8, minutes=index),
            )
            for index in range(3)
        ]
        messages.extend(
            Message(
                tenant_id=tenant.id,
                conversation_id=target.id,
                direction="in",
                sender_id="bob",
                sender_name="旧名称 Bob",
                content=f"Bob 消息 {index}",
                created_at=yesterday + timedelta(hours=9, minutes=index),
            )
            for index in range(2)
        )
        messages.extend(
            [
                Message(
                    tenant_id=tenant.id,
                    conversation_id=target.id,
                    direction="out",
                    sender_id="alice",
                    sender_name="爱丽丝",
                    content="机器人出站消息",
                    created_at=yesterday + timedelta(hours=10),
                ),
                Message(
                    tenant_id=tenant.id,
                    conversation_id=other.id,
                    direction="in",
                    sender_id="mallory",
                    sender_name="其他群成员",
                    content="其他群消息",
                    created_at=yesterday + timedelta(hours=10),
                ),
                Message(
                    tenant_id=tenant.id,
                    conversation_id=target.id,
                    direction="in",
                    sender_id="alice",
                    sender_name="爱丽丝",
                    content="今日消息",
                    created_at=FIXED_NOW.replace(hour=8),
                ),
            ]
        )
        db.add_all(messages)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "binding_id": int(binding.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(target.id),
        }
    try:
        yield factory, identity, tmp_path
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(identity: dict[str, int], action: str) -> PluginExecutionRequest:
    """构造与统一调度器一致的可信定时执行请求。"""

    return PluginExecutionRequest(
        action=action,
        config={
            "enabled": True,
            "display_limit": 10,
            "reward_limit": 20,
            "render_as_image": True,
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


def test_daily_ranking_manifest_is_scheduled_and_has_no_chat_matcher() -> None:
    """每日排行只能由统一调度器触发，不能重新注册聊天命令入口。"""

    discovered = get_discovered_plugin("daily_ranking", force=True)

    assert discovered is not None
    assert discovered.is_valid is True
    assert discovered.manifest.plugin_type == "scheduled"
    assert discovered.matcher.commands == ()
    assert discovered.matcher.keywords == ()
    assert discovered.runtime_config["daily_ranking_conversation_ids"] == []
    assert discovered.runtime_config["prepare_time"] == "04:30"
    assert discovered.runtime_config["push_time"] == "09:30"


def test_prepare_uses_saas_messages_and_chatroom_members(daily_ranking_fixture, monkeypatch) -> None:
    """预生成只缓存结构化日榜，并优先使用 SaaS 群成员显示名。"""

    factory, identity, _tmp_path = daily_ranking_fixture
    loaded = load_directory_plugin("daily_ranking", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = loaded.instance.execute_scheduled_action(
            db,
            _request(identity, "daily_ranking_prepare"),
        )
        db.commit()
        state = db.scalar(select(PluginStateRecord))

        assert result.ok is True
        assert result.status == "completed"
        assert result.output["date_key"] == "2026-08-31"
        assert result.output["message_count"] == 5
        assert result.output["participant_count"] == 2
        assert state is not None
        payload = json.loads(state.payload_json)
        assert payload["total_messages"] == 5
        assert payload["participant_count"] == 2
        assert payload["member_count"] == 2
        assert [(row["user_id"], row["display_name"], row["message_count"]) for row in payload["rows"]] == [
            ("alice", "爱丽丝", 3),
            ("bob", "小波", 2),
        ]
        assert [row["reward_points"] for row in payload["rows"]] == [30, 20]


def test_push_reuses_cache_and_keeps_rewards_and_outbound_idempotent(
    daily_ranking_fixture,
    monkeypatch,
) -> None:
    """重复推送不得重复发积分或创建第二个出站任务。"""

    factory, identity, tmp_path = daily_ranking_fixture
    loaded = load_directory_plugin("daily_ranking", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    image_root = tmp_path / "md2image"
    image_root.mkdir(parents=True)

    def fake_render(_markdown: str, output_name: str, *, base_dir: Path) -> str:
        del base_dir
        output_path = image_root / output_name
        output_path.write_bytes(b"daily-ranking-image")
        return str(output_path)

    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)
    monkeypatch.setattr(module, "RANKING_IMAGE_ROOT", image_root)
    monkeypatch.setattr(module, "convert_md_str_to_image_sync", fake_render)

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        prepare = loaded.instance.execute_scheduled_action(db, _request(identity, "daily_ranking_prepare"))
        first = loaded.instance.execute_scheduled_action(db, _request(identity, "daily_ranking_push"))
        second = loaded.instance.execute_scheduled_action(db, _request(identity, "daily_ranking_push"))

        assert prepare.ok is True
        assert first.ok is True
        assert first.output["cache_hit"] is True
        assert first.output["rewarded_user_count"] == 2
        assert second.ok is True
        assert second.output["outbound_job_id"] == first.output["outbound_job_id"]
        assert db.scalar(select(func.count(PointsTransaction.id))) == 2
        assert db.scalar(select(func.count(TaskJob.id))) == 1
        accounts = db.scalars(select(PointsAccount).order_by(PointsAccount.user_id.asc())).all()
        assert [(item.user_id, item.total_points) for item in accounts] == [("alice", 30), ("bob", 20)]
        job = db.get(TaskJob, int(first.output["outbound_job_id"]))
        assert job is not None
        outbound = parse_job_payload(job)["payload"]
        assert outbound["message_type"] == "image"
        assert outbound["base64"]
        assert not list(image_root.glob("*.png"))


def test_push_falls_back_to_business_text_when_image_render_fails(
    daily_ranking_fixture,
    monkeypatch,
) -> None:
    """图片环境不可用时仍发送完整业务榜单，不向用户暴露渲染异常。"""

    factory, identity, tmp_path = daily_ranking_fixture
    loaded = load_directory_plugin("daily_ranking", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)
    monkeypatch.setattr(module, "RANKING_IMAGE_ROOT", tmp_path / "md2image")
    monkeypatch.setattr(
        module,
        "convert_md_str_to_image_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("chromium unavailable")),
    )

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = loaded.instance.execute_scheduled_action(db, _request(identity, "daily_ranking_push"))
        job = db.get(TaskJob, int(result.output["outbound_job_id"]))
        assert job is not None
        outbound = parse_job_payload(job)["payload"]

        assert result.ok is True
        assert result.output["reply_type"] == "text"
        assert outbound["message_type"] == "text"
        assert "2026-08-31 群聊活跃榜" in outbound["content"]
        assert "爱丽丝：3 条，奖励 30 积分" in outbound["content"]
        assert "chromium" not in outbound["content"]
