from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "existing-query-plugins"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, Channel, Conversation, TaskJob, WeatherSubscription
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


def _weather_snapshot() -> dict[str, object]:
    """构造只包含平台允许持久化字段的天气快照。"""

    return {
        "city_id": "101250101",
        "city_name": "长沙",
        "adm1": "湖南",
        "country": "中国",
        "forecast_date": "2026-09-01",
        "condition_day": "多云",
        "condition_night": "小雨",
        "temp_min": 20,
        "temp_max": 33,
        "humidity": 70,
        "precipitation": 3.2,
        "uv_index": 8,
        "sunrise": "06:10",
        "sunset": "18:55",
        "wind_direction": "南风",
        "wind_scale": "3-4",
    }


@pytest.fixture()
def weather_fixture(tmp_path: Path):
    """准备两个租户的机器人和会话，用于验证订阅只接受可信上下文。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'weather.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        alpha = Tenant(code="alpha-weather", name="甲租户", status="active")
        beta = Tenant(code="beta-weather", name="乙租户", status="active")
        db.add_all([alpha, beta])
        db.flush()
        alpha_bot = Bot(tenant_id=alpha.id, name="甲机器人", status="running", is_enabled=True)
        beta_bot = Bot(tenant_id=beta.id, name="乙机器人", status="running", is_enabled=True)
        db.add_all([alpha_bot, beta_bot])
        db.flush()
        alpha_channel = Channel(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            name="甲 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        beta_channel = Channel(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            name="乙 Telegram",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add_all([alpha_channel, beta_channel])
        db.flush()
        alpha_conversation = Conversation(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            channel_id=alpha_channel.id,
            channel_type="telegram",
            external_id="room-alpha-weather",
            chat_type="group",
        )
        beta_conversation = Conversation(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            channel_id=beta_channel.id,
            channel_type="telegram",
            external_id="room-beta-weather",
            chat_type="group",
        )
        db.add_all([alpha_conversation, beta_conversation])
        db.commit()
        identity = {
            "alpha_tenant_id": int(alpha.id),
            "alpha_bot_id": int(alpha_bot.id),
            "alpha_channel_id": int(alpha_channel.id),
            "alpha_conversation_id": int(alpha_conversation.id),
            "beta_conversation_id": int(beta_conversation.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_weather_uses_latest_commands_and_clean_tenant_config() -> None:
    """天气插件只保留当前命令，增加 Telegram，并删除固定地址和旧任务配置。"""

    discovered = get_discovered_plugin("weather", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("天气", "订阅天气", "取消订阅")
    assert discovered.matcher.keywords == ("天气",)
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config["api_base"] == ""
    assert discovered.runtime_config["daily_push_time"] == "08:00"
    assert "units" not in discovered.runtime_config
    assert "default_timeout" not in discovered.runtime_config
    assert "default_push_hour" not in discovered.runtime_config

    loaded = load_directory_plugin("weather", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    suffix_context = type(
        "SuffixContext",
        (),
        {"chat_type": "group", "message_type": "text", "content": "长沙天气"},
    )()
    removed_context = type(
        "RemovedContext",
        (),
        {"chat_type": "group", "message_type": "text", "content": "天气订阅"},
    )()
    assert module.matches_event(suffix_context)["matched"] is True
    assert module.matches_event(removed_context)["matched"] is False

    source = (PROJECT_ROOT / "plugins/weather/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "urlrequest" not in source
    assert "WEATHER_PUSH_JOB" not in source
    assert "process_weather_push_job" not in source
    assert "_serialize_config" not in source


def test_weather_query_uses_controlled_http_and_keeps_output_minimal() -> None:
    """查询只调用租户配置的和风天气契约，结果不保存密钥或上游完整响应。"""

    loaded = load_directory_plugin("weather", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    captured_urls: list[str] = []

    class FakeHttpClient:
        def request(self, method, url, **kwargs):  # noqa: ANN001
            captured_urls.append(url)
            if "/geo/v2/city/lookup" in url:
                payload = {
                    "code": "200",
                    "location": [{"id": "101250101", "name": "长沙", "adm1": "湖南", "country": "中国"}],
                    "provider_trace": "must-not-persist",
                }
            else:
                payload = {
                    "code": "200",
                    "daily": [
                        {
                            "fxDate": "2026-09-01",
                            "textDay": "多云",
                            "textNight": "小雨",
                            "tempMin": "20",
                            "tempMax": "33",
                            "humidity": "70",
                            "precip": "3.2",
                            "uvIndex": "8",
                            "sunrise": "06:10",
                            "sunset": "18:55",
                            "windDirDay": "南风",
                            "windScaleDay": "3-4",
                        }
                    ],
                    "provider_trace": "must-not-persist",
                }
            return PluginHttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                url=url,
            )

    request = PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "api_base": "https://weather.example.test",
            "api_key": "tenant-weather-key",
            "default_city": "北京",
        },
        payload={"content": "/天气 长沙", "command_name": "天气", "command_args": "长沙"},
    )
    with patch.object(module, "PluginHttpClient", return_value=FakeHttpClient()):
        result = module.execute_action(request)

    assert result.ok is True
    assert len(captured_urls) == 2
    assert "/geo/v2/city/lookup" in captured_urls[0]
    assert "/v7/weather/3d" in captured_urls[1]
    assert "tenant-weather-key" in captured_urls[0]
    serialized = json.dumps(result.output, ensure_ascii=False)
    assert "tenant-weather-key" not in serialized
    assert "provider_trace" not in serialized
    assert result.output["city"] == "长沙"
    assert result.output["temp_min"] == 20
    assert result.output["temp_max"] == 33
    assert "长沙天气" in result.output["replies"][0]["content"]


def test_weather_subscription_uses_trusted_context_and_unified_daily_push(weather_fixture) -> None:
    """订阅忽略伪造会话编号，并由统一定时入口按城市、会话去重推送。"""

    factory, identity = weather_fixture
    loaded = load_directory_plugin("weather", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    config = {
        "enabled": True,
        "api_base": "https://weather.example.test",
        "api_key": "tenant-weather-key",
        "default_city": "北京",
        "daily_push_time": "08:00",
    }
    context = PluginExecutionContext(
        tenant_id=identity["alpha_tenant_id"],
        binding_id=501,
        bot_id=identity["alpha_bot_id"],
        channel_id=identity["alpha_channel_id"],
        conversation_id=identity["alpha_conversation_id"],
        sender_id="member-alpha-1",
        channel_type="telegram",
        chat_type="group",
        message_type="text",
    )
    subscribe_request = PluginExecutionRequest(
        action="execute",
        config=config,
        payload={
            "content": "订阅天气 长沙",
            "command_name": "订阅天气",
            "command_args": "长沙",
            # 业务载荷故意伪造另一个租户的会话；实现只能使用上面的可信 context。
            "conversation_id": identity["beta_conversation_id"],
        },
        context=context,
    )
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        with patch.object(module, "_fetch_weather_bundle", return_value=_weather_snapshot()):
            subscribed = module.execute_message_action(db, subscribe_request)
        assert subscribed.ok is True
        first = db.scalar(select(WeatherSubscription).where(WeatherSubscription.subscriber_id == "member-alpha-1"))
        assert first is not None
        assert first.tenant_id == identity["alpha_tenant_id"]
        assert first.bot_id == identity["alpha_bot_id"]
        assert first.conversation_id == identity["alpha_conversation_id"]
        assert first.config_json is None
        assert "tenant-weather-key" not in str(first.latest_snapshot_json or "")

        second = WeatherSubscription(
            tenant_id=identity["alpha_tenant_id"],
            bot_id=identity["alpha_bot_id"],
            channel_id=identity["alpha_channel_id"],
            conversation_id=identity["alpha_conversation_id"],
            chat_type="group",
            target_external_id="room-alpha-weather",
            subscriber_id="member-alpha-2",
            city_query="长沙",
            city_name="长沙",
            city_id="101250101",
            schedule_hour=8,
            status="active",
            config_json=json.dumps({"api_key": "legacy-plaintext-key"}),
            latest_snapshot_json=json.dumps({"temp_max": 25}, ensure_ascii=False),
        )
        db.add(second)
        db.flush()

        scheduled_request = PluginExecutionRequest(
            action="weather_daily_push",
            config=config,
            context=PluginExecutionContext(
                tenant_id=identity["alpha_tenant_id"],
                binding_id=501,
                bot_id=identity["alpha_bot_id"],
            ),
        )
        with patch.object(module, "_fetch_weather_by_location", return_value=_weather_snapshot()):
            scheduled = module.execute_scheduled_action(db, scheduled_request)
        assert scheduled.ok is True
        assert scheduled.output["requested_city_count"] == 1
        assert scheduled.output["queued_conversation_count"] == 1
        assert scheduled.output["successful_subscription_count"] == 2
        jobs = db.scalars(select(TaskJob).where(TaskJob.related_type == "weather_subscription")).all()
        assert len(jobs) == 1
        assert "tenant-weather-key" not in str(jobs[0].payload_json or "")
        assert all(item.config_json is None for item in (first, second))

        unsubscribe_request = PluginExecutionRequest(
            action="execute",
            config=config,
            payload={"content": "取消订阅", "command_name": "取消订阅"},
            context=context,
        )
        unsubscribed = module.execute_message_action(db, unsubscribe_request)
        assert unsubscribed.ok is True
        assert first.status == "cancelled"
        assert second.status == "active"


def test_music_uses_tenant_service_and_cross_channel_file_replies() -> None:
    """点歌删除固定内网地址和微信 XML，改用租户服务与标准文件回复。"""

    discovered = get_discovered_plugin("music", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("点歌", "音乐", "音乐点播", "点播音乐", "音乐点歌")
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config["api_base"] == ""
    assert discovered.runtime_config["quality"] == "exhigh"
    assert "music_api_url" not in discovered.runtime_config
    assert "song_api_url" not in discovered.runtime_config

    loaded = load_directory_plugin("music", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    captured: list[dict[str, object]] = []

    class FakeHttpClient:
        def request(self, method, url, **kwargs):  # noqa: ANN001
            captured.append({"method": method, "url": url, "kwargs": kwargs})
            if "/Search?" in url:
                payload = {
                    "success": True,
                    "status": 200,
                    "data": [
                        {
                            "id": "song-001",
                            "name": "夜空中最亮的星",
                            "artist_string": "逃跑计划",
                            "provider_trace": "must-not-persist",
                        }
                    ],
                }
            else:
                payload = {
                    "success": True,
                    "status": 200,
                    "data": {
                        "name": "夜空中最亮的星",
                        "ar_name": "逃跑计划",
                        "url": "https://media.example.test/song-001",
                        "pic": "https://media.example.test/song-001.jpg",
                        "provider_trace": "must-not-persist",
                    },
                }
            return PluginHttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                url=url,
            )

    request = PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "api_base": "https://music.example.test",
            "api_key": "tenant-music-key",
            "quality": "lossless",
        },
        payload={"content": "/点歌 夜空中最亮的星", "command_args": "夜空中最亮的星"},
    )
    with patch.object(module, "PluginHttpClient", return_value=FakeHttpClient()):
        result = module.execute_action(request)

    assert result.ok is True
    assert len(captured) == 2
    assert "/Search?" in str(captured[0]["url"])
    assert "/Song_V1?" in str(captured[1]["url"])
    assert captured[0]["kwargs"]["headers"]["Authorization"] == "Bearer tenant-music-key"
    assert result.output["replies"][0]["type"] == "text"
    assert result.output["replies"][1] == {
        "type": "file",
        "file_name": "逃跑计划 - 夜空中最亮的星.mp3",
        "file_url": "https://media.example.test/song-001",
    }
    serialized = json.dumps(result.output, ensure_ascii=False)
    assert "tenant-music-key" not in serialized
    assert "provider_trace" not in serialized
    assert "music_xml" not in serialized

    source = (PROJECT_ROOT / "plugins/music/plugin.py").read_text(encoding="utf-8")
    assert "urlrequest" not in source
    assert "MUSIC_XML" not in source
    assert "192.168.2.240" not in source


def test_music_rejects_missing_service_and_invalid_play_url() -> None:
    """未配置租户服务时直接返回业务错误，非 HTTP 播放地址不能进入出站任务。"""

    loaded = load_directory_plugin("music", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    missing = module.execute_action(
        PluginExecutionRequest(
            action="execute",
            config={"enabled": True, "api_base": ""},
            payload={"content": "点歌 测试歌曲"},
        )
    )
    assert missing.ok is False
    assert missing.output["provider_status"] == "music_not_configured"

    assert module._public_http_url("file:///tmp/song.mp3") == ""
    assert module._public_http_url("https://user:pass@media.example.test/song.mp3") == ""
