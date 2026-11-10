from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-dota2-stats"
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
    TaskJob,
)
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse
from app.services.plugin_runtime.state_store import PluginStateStore
from app.services.task_queue import parse_job_payload


class FakeOpenDotaClient:
    """返回稳定 OpenDota 数据，并记录插件是否只访问官方固定接口。"""

    def __init__(self):
        self.urls: list[str] = []

    def request(self, method: str, url: str, **_kwargs) -> PluginHttpResponse:
        assert method == "GET"
        self.urls.append(url)
        parsed = urlsplit(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "api.opendota.com"
        assert parse_qs(parsed.query).get("api_key") == ["tenant-opendota-key"]
        path = parsed.path
        if path.endswith("/constants/heroes"):
            payload: object = {
                "1": {"id": 1, "localized_name": "敌法师"},
                "2": {"id": 2, "localized_name": "斧王"},
            }
        elif path.endswith("/recentMatches"):
            payload = _matches()
        elif path.endswith("/matches"):
            payload = _matches()
        elif path.endswith("/wl"):
            payload = {"win": 120, "lose": 80}
        elif "/players/" in path:
            payload = {
                "profile": {"personaname": "Dota 测试玩家"},
                "rank_tier": 53,
                "leaderboard_rank": 0,
            }
        else:
            raise AssertionError(f"unexpected OpenDota path: {path}")
        return PluginHttpResponse(
            status_code=200,
            headers={"content-type": "application/json; charset=utf-8"},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            url=url,
        )


def _matches() -> list[dict[str, object]]:
    """构造两场位于统计窗口内且胜负相反的公开比赛。"""

    first_time = int(datetime(2026, 8, 31, 20, 0, tzinfo=SHANGHAI_TZ).timestamp())
    second_time = int(datetime(2026, 8, 30, 20, 0, tzinfo=SHANGHAI_TZ).timestamp())
    return [
        {
            "match_id": 1001,
            "player_slot": 0,
            "radiant_win": True,
            "hero_id": 1,
            "kills": 12,
            "deaths": 3,
            "assists": 9,
            "duration": 2400,
            "start_time": first_time,
            "game_mode": 22,
            "lobby_type": 7,
            "gold_per_min": 650,
            "xp_per_min": 700,
        },
        {
            "match_id": 1002,
            "player_slot": 128,
            "radiant_win": True,
            "hero_id": 2,
            "kills": 4,
            "deaths": 8,
            "assists": 10,
            "duration": 1800,
            "start_time": second_time,
            "game_mode": 23,
            "lobby_type": 0,
            "gold_per_min": 420,
            "xp_per_min": 480,
        },
    ]


@pytest.fixture()
def dota_fixture(tmp_path: Path):
    """建立群聊、两个成员及三条命令消息，覆盖绑定、查询和群榜读取。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'dota2-stats.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="dota2", name="Dota 租户", status="active")
        plugin = Plugin(code="dota2_stats", display_name="Dota 2 战绩", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="Dota 机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="Dota Telegram",
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
            external_id="dota-room",
            title="Dota 交流群",
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
                {
                    "enabled": True,
                    "dota2_group_ranking_conversation_ids": [conversation.id],
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
                    member_external_id="user-a",
                    display_name="甲玩家",
                    status="active",
                ),
                ChatroomMember(
                    tenant_id=tenant.id,
                    chatroom_id=chatroom.id,
                    bot_id=bot.id,
                    channel_id=channel.id,
                    member_external_id="user-b",
                    display_name="乙玩家",
                    status="active",
                ),
            ]
        )
        messages = [
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction="in",
                sender_id="user-a",
                sender_name="甲玩家",
                content="注册Dota 76561197960265851",
                external_message_id="register-1",
            ),
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction="in",
                sender_id="user-a",
                sender_name="甲玩家",
                content="Dota战绩",
                external_message_id="query-1",
            ),
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                direction="in",
                sender_id="user-b",
                sender_name="乙玩家",
                content="Dota群排行",
                external_message_id="ranking-1",
            ),
        ]
        db.add_all(messages)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "binding_id": int(binding.id),
            "bot_id": int(bot.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "register_message_id": int(messages[0].id),
            "query_message_id": int(messages[1].id),
            "ranking_message_id": int(messages[2].id),
        }
    try:
        yield factory, identity, tmp_path
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(
    identity: dict[str, int],
    *,
    message_id: int | None,
    sender_id: str | None,
    action: str = "execute",
) -> PluginExecutionRequest:
    """构造与消息分发或统一调度一致的可信插件请求。"""

    return PluginExecutionRequest(
        action=action,
        config={
            "enabled": True,
            "api_key": "tenant-opendota-key",
            "summary_match_limit": 20,
            "recent_match_limit": 10,
            "group_ranking_window_days": 7,
            "group_ranking_limit": 10,
            "minimum_win_rate_matches": 1,
            "max_group_players": 50,
            "render_as_image": True,
        },
        payload={},
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=message_id,
            sender_id=sender_id,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def test_dota2_manifest_uses_tenant_secret_and_current_commands() -> None:
    """Dota 插件保留三个业务命令，OpenDota Key 不得进入普通默认配置。"""

    discovered = get_discovered_plugin("dota2_stats", force=True)

    assert discovered is not None
    assert discovered.is_valid is True
    assert discovered.matcher.commands == ("注册Dota", "Dota战绩", "Dota群排行")
    assert discovered.secret_fields == ("api_key",)
    assert "api_key" not in discovered.runtime_config
    assert 'api_key = ""' in (PROJECT_ROOT / "plugins" / "dota2_stats" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert "base_url" not in discovered.runtime_config
    assert discovered.runtime_config["dota2_group_ranking_conversation_ids"] == []


def test_register_and_bound_query_use_official_opendota_and_state(
    dota_fixture,
    monkeypatch,
) -> None:
    """注册接受 SteamID64，查询复用租户绑定，并在图片失败时返回完整文本。"""

    factory, identity, _tmp_path = dota_fixture
    loaded = load_directory_plugin("dota2_stats", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_client = FakeOpenDotaClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_client)
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)
    monkeypatch.setattr(
        module,
        "convert_md_str_to_image_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render unavailable")),
    )

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        registered = loaded.instance.execute_message_action(
            db,
            _request(
                identity,
                message_id=identity["register_message_id"],
                sender_id="user-a",
            ),
        )
        queried = loaded.instance.execute_message_action(
            db,
            _request(
                identity,
                message_id=identity["query_message_id"],
                sender_id="user-a",
            ),
        )
        db.commit()
        binding_state = db.scalar(
            select(PluginStateRecord).where(PluginStateRecord.namespace == "dota2_user_bindings")
        )

        assert registered.ok is True
        assert "Account ID：123" in registered.output["replies"][0]["content"]
        assert binding_state is not None
        assert json.loads(binding_state.payload_json)["account_id"] == 123
        assert queried.ok is True
        assert queried.output["reply_type"] == "text"
        report_text = queried.output["replies"][0]["content"]
        assert "Dota 2 最近战绩｜Dota 测试玩家" in report_text
        assert "近2场：1胜 1负" in report_text
        assert "敌法师" in report_text
        assert all(url.startswith("https://api.opendota.com/api/") for url in fake_client.urls)


def test_group_prepare_deduplicates_accounts_and_push_reads_snapshot_only(
    dota_fixture,
    monkeypatch,
) -> None:
    """群榜准备按 Account ID 去重联网，命令和推送阶段都不得再次访问 OpenDota。"""

    factory, identity, tmp_path = dota_fixture
    loaded = load_directory_plugin("dota2_stats", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fake_client = FakeOpenDotaClient()
    monkeypatch.setattr(module, "PluginHttpClient", lambda: fake_client)
    monkeypatch.setattr(module, "now_shanghai", lambda: FIXED_NOW)
    monkeypatch.setattr(module, "DOTA_IMAGE_ROOT", tmp_path / "md2image")
    monkeypatch.setattr(
        module,
        "convert_md_str_to_image_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render unavailable")),
    )

    scheduled_request = _request(identity, message_id=None, sender_id=None, action="dota2_group_ranking_prepare")
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        # 两个群成员绑定同一个 Dota 账号，准备任务只应请求一份玩家和比赛数据。
        state_context = scheduled_request.context
        assert state_context is not None
        store = PluginStateStore(db, state_context)
        store.put(
            "dota2_user_bindings",
            module._user_binding_state_key("user-a"),
            {"user_id": "user-a", "account_id": 123, "player_name": "甲 Dota"},
        )
        store.put(
            "dota2_user_bindings",
            module._user_binding_state_key("user-b"),
            {"user_id": "user-b", "account_id": 123, "player_name": "乙 Dota"},
        )
        db.commit()

        prepared = loaded.instance.execute_scheduled_action(db, scheduled_request)
        request_count_after_prepare = len(fake_client.urls)
        assert prepared.ok is True
        assert prepared.output["bound_user_count"] == 2
        assert prepared.output["unique_account_count"] == 1
        assert request_count_after_prepare == 2

        # 将 HTTP 客户端替换为立即失败实现，证明群命令和推送只消费当天快照。
        monkeypatch.setattr(
            module,
            "PluginHttpClient",
            lambda: (_ for _ in ()).throw(AssertionError("snapshot path must not access OpenDota")),
        )
        ranking = loaded.instance.execute_message_action(
            db,
            _request(
                identity,
                message_id=identity["ranking_message_id"],
                sender_id="user-b",
            ),
        )
        push_request = _request(identity, message_id=None, sender_id=None, action="dota2_group_ranking_push")
        first_push = loaded.instance.execute_scheduled_action(db, push_request)
        second_push = loaded.instance.execute_scheduled_action(db, push_request)

        assert ranking.ok is True
        assert "Dota 交流群 Dota 2 群排行" in ranking.output["replies"][0]["content"]
        assert first_push.ok is True
        assert first_push.output["reply_type"] == "text"
        assert second_push.output["outbound_job_id"] == first_push.output["outbound_job_id"]
        assert db.scalar(select(func.count(TaskJob.id))) == 1
        job = db.get(TaskJob, int(first_push.output["outbound_job_id"]))
        assert job is not None
        outbound = parse_job_payload(job)["payload"]
        assert outbound["message_type"] == "text"
        assert "近 7 天" in outbound["content"]
        assert len(fake_client.urls) == request_count_after_prepare
