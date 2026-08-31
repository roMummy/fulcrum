from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "existing-ai-content-plugins"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Plugin, TaskJob
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


@pytest.fixture()
def daily_news_fixture(tmp_path: Path):
    """建立一个群聊和一个私聊，验证日报只向群聊创建任务。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'daily-news.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="daily", name="日报租户", status="active")
        plugin = Plugin(code="daily_news", display_name="每日新闻", plugin_type="scheduled")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="日报机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        group_channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="群渠道",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        direct_channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="私聊渠道",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add_all([group_channel, direct_channel])
        db.flush()
        group = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=group_channel.id,
            channel_type="telegram",
            external_id="group-1",
            chat_type="group",
        )
        direct = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=direct_channel.id,
            channel_type="telegram",
            external_id="user-1",
            chat_type="direct",
        )
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json=json.dumps({"push_time": "08:30", "last_push_date": ""}),
        )
        db.add_all([group, direct, binding])
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "binding_id": int(binding.id),
            "group_conversation_id": int(group.id),
            "direct_conversation_id": int(direct.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_daily_news_matches_latest_scheduled_only_contract() -> None:
    """每日新闻不再暴露旧手工命令或非群聊能力。"""

    discovered = get_discovered_plugin("daily_news", force=True)
    assert discovered is not None
    assert discovered.manifest.plugin_type == "scheduled"
    assert discovered.matcher.commands == ()
    assert discovered.capability.chat_types == ("group",)

    source = (PROJECT_ROOT / "plugins/daily_news/plugin.py").read_text(encoding="utf-8")
    assert 'SCHEDULE_ACTION_KEY = "baidu_news_daily_push"' in source
    assert "SessionLocal" not in source
    assert "important_news" not in source


def test_daily_news_scheduled_action_queues_text_and_image_for_groups_only(daily_news_fixture) -> None:
    """统一调度入口只向当前机器人的启用群聊创建标准出站任务。"""

    factory, identity = daily_news_fixture
    loaded = load_directory_plugin("daily_news", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    image_base64 = base64.b64encode(b"daily-image").decode("ascii")

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        request = PluginExecutionRequest(
            action="baidu_news_daily_push",
            config={"push_time": "08:30", "last_push_date": ""},
            payload={},
            context=PluginExecutionContext(
                tenant_id=identity["tenant_id"],
                binding_id=identity["binding_id"],
                bot_id=identity["bot_id"],
            ),
        )
        with patch.object(
            module,
            "_build_daily_digest",
            return_value={"text": "今日热榜", "image_base64": image_base64},
        ):
            result = module.execute_scheduled_action(db, request)

        assert result.ok is True
        assert result.status == "completed"
        jobs = db.scalars(select(TaskJob).order_by(TaskJob.id.asc())).all()
        assert len(jobs) == 2
        payloads = [json.loads(job.payload_json) for job in jobs]
        assert {item["conversation_id"] for item in payloads} == {identity["group_conversation_id"]}
        assert {item["payload"]["message_type"] for item in payloads} == {"text", "image"}
        binding = db.get(BotPlugin, identity["binding_id"])
        assert binding is not None
        assert json.loads(binding.config_json or "{}")["last_push_date"]
        assert identity["direct_conversation_id"] not in {item["conversation_id"] for item in payloads}


def test_daily_news_baidu_format_keeps_latest_hashtag_output() -> None:
    """百度热榜保持来源中的日期、编号、井号和空格转下划线格式。"""

    loaded = load_directory_plugin("daily_news", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json; charset=utf-8"},
        body=json.dumps(
            {
                "data": {
                    "cards": [
                        {"content": [{"content": [{"word": "第一 条"}, {"word": "第二条"}]}]}
                    ]
                }
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        url="https://example.test/news",
    )
    fake_client = type("FakeClient", (), {"request": lambda self, *args, **kwargs: response})()
    config = module._load_config({"baidu_limit": 2})
    with patch.object(module, "PluginHttpClient", return_value=fake_client):
        text = module._get_baidu_news(config, current=datetime(2026, 8, 31, 9, 0))

    assert "当前日期：2026年08月31日  星期一" in text
    assert "1 :#第一_条" in text
    assert "2 :#第二条" in text
