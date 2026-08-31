from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-epic-free"

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
    Plugin,
    PluginStateRecord,
    TaskJob,
)
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse
from app.services.plugin_runtime.scheduler import schedule_due_plugin_actions


FIXED_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def epic_free_fixture(tmp_path: Path):
    """建立一个启用群、一个群级停用群和一个私聊，验证定时目标边界。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'epic-free.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="epic-free", name="Epic 租户", status="active")
        plugin = Plugin(code="epic_free", display_name="Epic 免费游戏", plugin_type="scheduled")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="Epic 机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        telegram = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="Epic Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        ipad = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="Epic iPad",
            channel_type="wechat_ipad",
            status="online",
            is_enabled=True,
        )
        db.add_all([telegram, ipad])
        db.flush()
        enabled_group = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=telegram.id,
            channel_type="telegram",
            external_id="epic-enabled-group",
            title="启用群",
            chat_type="group",
        )
        disabled_group = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=ipad.id,
            channel_type="wechat_ipad",
            external_id="epic-disabled@chatroom",
            title="停用群",
            chat_type="group",
        )
        direct = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=telegram.id,
            channel_type="telegram",
            external_id="epic-direct",
            title="私聊",
            chat_type="direct",
        )
        db.add_all([enabled_group, disabled_group, direct])
        db.flush()
        chatroom = Chatroom(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=ipad.id,
            external_id="epic-disabled@chatroom",
            name="停用群",
            is_active=True,
        )
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json=json.dumps({"poll_interval_seconds": 3600}, ensure_ascii=False),
        )
        db.add_all([chatroom, binding])
        db.flush()
        db.add(
            GroupPluginScope(
                tenant_id=tenant.id,
                channel_id=ipad.id,
                chatroom_id=chatroom.id,
                bot_plugin_id=binding.id,
                scope_status="manual",
                enabled=False,
            )
        )
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "binding_id": int(binding.id),
            "enabled_conversation_id": int(enabled_group.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


class FakeEpicClient:
    """返回可变官网响应，并记录插件是否只访问固定 Epic 地址。"""

    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.urls: list[str] = []

    def request(self, method: str, url: str, **_kwargs) -> PluginHttpResponse:
        assert method == "GET"
        self.urls.append(url)
        return PluginHttpResponse(
            status_code=200,
            headers={"content-type": "application/json; charset=utf-8"},
            body=json.dumps(self.payload, ensure_ascii=False).encode("utf-8"),
            url=url,
        )


def _epic_payload(*, current_title: str = "当前游戏", upcoming_title: str = "下周游戏") -> dict[str, object]:
    """构造一个当前免费和一个即将免费的官方响应片段。"""

    def element(offer_id: str, title: str, bucket: str, start: str, end: str) -> dict[str, object]:
        promotions = {"promotionalOffers": [], "upcomingPromotionalOffers": []}
        promotions[bucket] = [
            {
                "promotionalOffers": [
                    {
                        "startDate": start,
                        "endDate": end,
                        "discountSetting": {"discountType": "PERCENTAGE", "discountPercentage": 0},
                    }
                ]
            }
        ]
        return {
            "id": offer_id,
            "title": title,
            "seller": {"name": "Epic Studio"},
            "productSlug": f"{offer_id}/home",
            "price": {
                "totalPrice": {"fmtPrice": {"originalPrice": "¥68", "discountPrice": "0"}}
            },
            "promotions": promotions,
        }

    return {
        "data": {
            "Catalog": {
                "searchStore": {
                    "elements": [
                        element(
                            "current-game",
                            current_title,
                            "promotionalOffers",
                            "2026-08-28T00:00:00.000Z",
                            "2026-09-03T00:00:00.000Z",
                        ),
                        element(
                            "upcoming-game",
                            upcoming_title,
                            "upcomingPromotionalOffers",
                            "2026-09-03T00:00:00.000Z",
                            "2026-09-10T00:00:00.000Z",
                        ),
                    ]
                }
            }
        }
    }


def test_epic_free_manual_query_uses_official_http_and_clean_config(monkeypatch) -> None:
    """人工查询只使用绑定地区语言和官方地址，不读取执行载荷覆盖参数。"""

    discovered = get_discovered_plugin("epic_free", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.manifest.plugin_type == "scheduled"
    assert discovered.matcher.commands == ()
    assert discovered.runtime_config["locale"] == "zh-CN"
    assert discovered.runtime_config["country"] == "CN"
    assert "telegram" in discovered.capability.channel_types

    loaded = load_directory_plugin("epic_free", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_client = FakeEpicClient(_epic_payload())
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_client)
    monkeypatch.setattr(module, "_now_utc", lambda: FIXED_NOW)
    result = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config=discovered.runtime_config,
            payload={"locale": "en-US", "country": "US", "api_url": "http://127.0.0.1/private"},
        )
    )

    assert result.ok is True and result.output["provider_status"] == "completed"
    assert result.output["current_count"] == 1
    assert result.output["upcoming_count"] == 1
    reply_text = result.output["replies"][0]["content"]
    assert "当前游戏" in reply_text and "下周游戏" in reply_text
    assert "地区：CN" in reply_text
    assert len(fake_client.urls) == 1
    assert fake_client.urls[0].startswith(
        "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions?"
    )
    assert "locale=zh-CN" in fake_client.urls[0]
    assert "country=CN" in fake_client.urls[0]
    source = (PROJECT_ROOT / "plugins/epic_free/plugin.py").read_text(encoding="utf-8")
    assert "requests" not in source
    assert "GroupBotManager" not in source
    assert "request.payload.get" not in source


def test_epic_free_scheduled_push_is_per_conversation_and_change_only(epic_free_fixture, monkeypatch) -> None:
    """同一摘要每群只投递一次，群级停用和私聊不会成为推送目标。"""

    factory, identity = epic_free_fixture
    loaded = load_directory_plugin("epic_free", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_client = FakeEpicClient(_epic_payload())
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_client)
    monkeypatch.setattr(module, "_now_utc", lambda: FIXED_NOW)
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)
    context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
    )
    request = PluginExecutionRequest(
        action="poll_free_games",
        config={
            "enabled": True,
            "locale": "zh-CN",
            "country": "CN",
            "include_upcoming": True,
            "only_on_change": True,
            "poll_interval_seconds": 3600,
        },
        payload={"conversation_id": 999, "country": "US"},
        context=context,
    )

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = module.execute_scheduled_action(db, request)
        repeated = module.execute_scheduled_action(db, request)
        fake_client.payload = _epic_payload(current_title="当前游戏更新")
        changed = module.execute_scheduled_action(db, request)
        jobs = db.scalars(select(TaskJob).where(TaskJob.related_type == "epic_free_binding")).all()
        states = db.scalars(
            select(PluginStateRecord).where(PluginStateRecord.namespace == "epic_free_delivery")
        ).all()

    assert first.ok is True and first.status == "completed"
    assert first.output["queued_conversation_count"] == 1
    assert repeated.ok is True and repeated.status == "skipped"
    assert changed.ok is True and changed.status == "completed"
    assert len(jobs) == 2
    assert {json.loads(item.payload_json)["conversation_id"] for item in jobs} == {
        identity["enabled_conversation_id"]
    }
    assert len(states) == 1
    assert "当前游戏更新" in json.loads(jobs[-1].payload_json)["payload"]["content"]


def test_epic_free_uses_unified_hourly_scheduler(epic_free_fixture) -> None:
    """统一调度器按时间桶只创建一个 Epic 轮询任务。"""

    factory, identity = epic_free_fixture
    scheduled_at = datetime(2026, 8, 31, 20, 37, tzinfo=timezone.utc)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        first = schedule_due_plugin_actions(db, now=scheduled_at)
        repeated = schedule_due_plugin_actions(db, now=scheduled_at)
        jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == "plugin_scheduled_action")).all()

    assert len(first) == 1
    assert repeated == []
    assert len(jobs) == 1
    payload = json.loads(jobs[0].payload_json)
    assert payload == {
        "binding_id": identity["binding_id"],
        "action_key": "poll_free_games",
        "parameters": {},
    }
