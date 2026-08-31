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


def test_ai_auto_response_is_group_only_without_manual_command() -> None:
    """AI 自动参与只进入群聊文本候选，不再暴露旧命令和私聊入口。"""

    discovered = get_discovered_plugin("ai_auto_response", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ()
    assert discovered.capability.chat_types == ("group",)
    assert "telegram" in discovered.capability.channel_types

    loaded = load_directory_plugin("ai_auto_response", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    group_context = type(
        "GroupContext",
        (),
        {
            "chat_type": "group",
            "message_type": "text",
            "content": "这个方案怎么样？",
            "command_name": "",
        },
    )()
    command_context = type(
        "CommandContext",
        (),
        {
            "chat_type": "group",
            "message_type": "text",
            "content": "/ai_auto_response 测试",
            "command_name": "ai_auto_response",
        },
    )()

    assert module.matches_event(group_context)["matched"] is True
    assert module.matches_event(command_context)["matched"] is False


def test_ai_auto_response_uses_controlled_http_and_only_returns_safe_decision() -> None:
    """群聊模型决策使用统一 HTTP，执行结果不持久化完整上游响应或租户密钥。"""

    loaded = load_directory_plugin("ai_auto_response", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    captured: dict[str, object] = {}
    decision_text = json.dumps(
        {
            "should_reply": True,
            "reply_mode": "qa_fast",
            "reply": "先把主流程跑通。",
            "topic_summary": "研发方案",
        },
        ensure_ascii=False,
    )
    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json; charset=utf-8"},
        body=json.dumps(
            {"data": {"outputs": {"result_json": decision_text}}, "trace_id": "sensitive-provider-trace"},
            ensure_ascii=False,
        ).encode("utf-8"),
        url="https://llm.example.test/v1/workflows/run",
    )

    class FakeClient:
        def post_json(self, url, payload, **kwargs):
            captured.update({"url": url, "payload": payload, "kwargs": kwargs})
            return response

    request = PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "api_base": "https://llm.example.test/v1",
            "api_key": "tenant-secret-key",
            "app_mode": "workflow",
            "persona_name": "小牛",
            "persona_style": "自然、简短、像群友",
            "max_reply_chars": 80,
        },
        payload={
            "content": "今天这个方案怎么样？",
            "messages": ["张三: 先做核心流程", "李四: 测试后面集中补"],
            "conversation_external_id": "group-room-1",
        },
        context=PluginExecutionContext(
            tenant_id=1,
            binding_id=2,
            bot_id=3,
            channel_id=4,
            conversation_id=5,
            message_id=6,
            sender_id="member-7",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    with patch.object(module, "PluginHttpClient", return_value=FakeClient()):
        result = module.execute_action(request)

    assert result.ok is True
    assert result.output == {
        "provider_status": "generated",
        "should_reply": True,
        "reply_mode": "qa_fast",
        "topic_summary": "研发方案",
        "history_size": 2,
        "reply_text": "先把主流程跑通。",
        "replies": [{"type": "text", "content": "先把主流程跑通。"}],
    }
    assert captured["url"] == "https://llm.example.test/v1/workflows/run"
    request_body = captured["payload"]
    assert isinstance(request_body, dict)
    workflow_inputs = request_body["inputs"]
    assert set(workflow_inputs) == {
        "persona",
        "group_profile",
        "context",
        "memory",
        "current_message",
        "control",
        "images",
    }
    assert "张三: 先做核心流程" in workflow_inputs["context"]
    assert "今天这个方案怎么样？" in workflow_inputs["current_message"]
    assert "model_decides_should_reply=true" in workflow_inputs["control"]
    assert "tenant-secret-key" not in json.dumps(result.output, ensure_ascii=False)
    assert "sensitive-provider-trace" not in json.dumps(result.output, ensure_ascii=False)


def test_ai_auto_response_honors_model_silence_and_rejects_direct_chat() -> None:
    """模型决定沉默时不生成回复，可信上下文不是群聊时不调用上游。"""

    loaded = load_directory_plugin("ai_auto_response", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {"answer": '{"should_reply": false, "reply": "", "reply_mode": "social_short"}'},
            ensure_ascii=False,
        ).encode("utf-8"),
        url="https://llm.example.test/v1/chat-messages",
    )

    class FakeClient:
        call_count = 0

        def post_json(self, *args, **kwargs):
            self.call_count += 1
            return response

    fake_client = FakeClient()
    base_request = {
        "action": "execute",
        "config": {
            "enabled": True,
            "api_base": "https://llm.example.test/v1",
            "api_key": "tenant-key",
            "app_mode": "chat",
        },
        "payload": {"content": "路过说一句", "messages": ["成员: 路过说一句"]},
    }
    with patch.object(module, "PluginHttpClient", return_value=fake_client):
        group_result = module.execute_action(
            PluginExecutionRequest(
                **base_request,
                context=PluginExecutionContext(
                    tenant_id=1,
                    binding_id=2,
                    bot_id=3,
                    chat_type="group",
                    message_type="text",
                ),
            )
        )
        direct_result = module.execute_action(
            PluginExecutionRequest(
                **base_request,
                context=PluginExecutionContext(
                    tenant_id=1,
                    binding_id=2,
                    bot_id=3,
                    chat_type="direct",
                    message_type="text",
                ),
            )
        )

    assert group_result.ok is True
    assert group_result.output["provider_status"] == "declined"
    assert group_result.output["replies"] == []
    assert direct_result.status == "ignored"
    assert fake_client.call_count == 1

    source = (PROJECT_ROOT / "plugins/ai_auto_response/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "urlrequest" not in source
    assert "provider_response" not in source


def test_dify_matches_latest_chat_command_and_group_mention_contract() -> None:
    """Dify 只保留来源最新“聊天”命令和群聊 @ 入口，不接管普通私聊。"""

    discovered = get_discovered_plugin("dify", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("聊天",)
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config["api_base"] == ""

    loaded = load_directory_plugin("dify", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    direct_plain = type(
        "DirectPlain",
        (),
        {"content": "普通消息", "command_name": "", "mentioned_self": False, "chat_type": "direct"},
    )()
    direct_command = type(
        "DirectCommand",
        (),
        {"content": "聊天 你好", "command_name": "聊天", "mentioned_self": False, "chat_type": "direct"},
    )()
    group_mention = type(
        "GroupMention",
        (),
        {"content": "@机器人 你好", "command_name": "", "mentioned_self": True, "chat_type": "group"},
    )()

    assert module.matches_event(direct_plain)["matched"] is False
    assert module.matches_event(direct_command)["matched"] is True
    assert module.matches_event(group_mention)["matched"] is True


def test_dify_uses_controlled_http_and_does_not_store_provider_payload() -> None:
    """Dify 对话通过统一 HTTP 执行，引用文本进入请求但原始响应和密钥不进入结果。"""

    loaded = load_directory_plugin("dify", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    captured: dict[str, object] = {}
    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "data": {
                    "outputs": {"text": "这是结合引用内容生成的回答。"},
                    "total_tokens": 123,
                },
                "trace_id": "provider-sensitive-trace",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        url="https://dify.example.test/v1/workflows/run",
    )

    class FakeClient:
        def post_json(self, url, payload, **kwargs):
            captured.update({"url": url, "payload": payload, "kwargs": kwargs})
            return response

    request = PluginExecutionRequest(
        action="execute",
        config={
            "api_base": "https://dify.example.test/v1",
            "api_key": "tenant-dify-key",
            "app_mode": "workflow",
            "request_timeout_seconds": 30,
        },
        payload={
            "content": "聊天 解释一下",
            "command_name": "聊天",
            "conversation_external_id": "room-1",
            "quote_payload": {"ref_type": 1, "ref_content": "被引用的原始内容"},
            "messages": ["成员A: 前一条", "成员B: 后一条"],
        },
        context=PluginExecutionContext(
            tenant_id=1,
            binding_id=2,
            bot_id=3,
            channel_id=4,
            conversation_id=5,
            message_id=6,
            sender_id="member-7",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    with patch.object(module, "PluginHttpClient", return_value=FakeClient()):
        result = module.execute_action(request)

    assert result.ok is True
    assert result.output == {
        "provider_status": "completed",
        "app_mode": "workflow",
        "reply_text": "这是结合引用内容生成的回答。",
        "media_type": "",
        "token_usage": 123,
        "quote_type": "text",
        "input_file_count": 0,
        "replies": [{"type": "text", "content": "这是结合引用内容生成的回答。"}],
    }
    assert captured["url"] == "https://dify.example.test/v1/workflows/run"
    request_body = captured["payload"]
    assert request_body["inputs"]["query"] == "被引用的原始内容"
    assert "成员A: 前一条" in request_body["inputs"]["history"]
    serialized_output = json.dumps(result.output, ensure_ascii=False)
    assert "tenant-dify-key" not in serialized_output
    assert "provider-sensitive-trace" not in serialized_output


def test_dify_converts_inline_image_to_generic_image_reply() -> None:
    """Dify 返回 data URL 图片时直接生成标准图片回复，不依赖某个渠道客户端。"""

    loaded = load_directory_plugin("dify", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    image_bytes = b"\x89PNG\r\n\x1a\n" + b"image-payload"
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps({"answer": f"data:image/png;base64,{image_base64}"}).encode("utf-8"),
        url="https://dify.example.test/v1/chat-messages",
    )

    class FakeClient:
        def post_json(self, *args, **kwargs):
            return response

    with patch.object(module, "PluginHttpClient", return_value=FakeClient()):
        result = module.execute_action(
            PluginExecutionRequest(
                action="execute",
                config={
                    "api_base": "https://dify.example.test/v1",
                    "api_key": "tenant-key",
                    "app_mode": "chat",
                },
                payload={"content": "聊天 画一张图", "command_name": "聊天"},
                context=PluginExecutionContext(
                    tenant_id=1,
                    binding_id=2,
                    bot_id=3,
                    sender_id="member-4",
                    channel_type="wechat_ipad",
                    chat_type="direct",
                    message_type="text",
                ),
            )
        )

    assert result.ok is True
    assert result.output["media_type"] == "image"
    assert result.output["reply_text"] == ""
    assert result.output["replies"] == [{"type": "image", "base64": image_base64}]

    source = (PROJECT_ROOT / "plugins/dify/plugin.py").read_text(encoding="utf-8")
    assert "WechatIpadApiClient" not in source
    assert "urlrequest" not in source
    assert "subprocess" not in source
    assert "provider_response" not in source


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


@pytest.fixture()
def douyu_schedule_fixture(tmp_path: Path):
    """建立一个斗鱼房间订阅，验证统一轮询动作的租户执行链路。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'douyu-schedule.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="douyu", name="斗鱼租户", status="active")
        plugin = Plugin(code="douyu", display_name="斗鱼", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="斗鱼机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="TG 群",
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
            external_id="room-douyu",
            chat_type="group",
        )
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "room_subscriptions": {"room-douyu": ["100"]},
                    "room_states": {"100": {"provider_status": "offline"}},
                    "poll_interval_seconds": 60,
                }
            ),
        )
        db.add_all([conversation, binding])
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "binding_id": int(binding.id),
            "conversation_id": int(conversation.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_douyu_scheduled_poll_uses_tenant_session_and_generic_text(douyu_schedule_fixture) -> None:
    """斗鱼统一轮询不再打开平台会话，Telegram 目标只收到通用文本任务。"""

    factory, identity = douyu_schedule_fixture
    loaded = load_directory_plugin("douyu", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    source = (PROJECT_ROOT / "plugins/douyu/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "poll_douyu_binding" not in source

    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, identity["binding_id"])
        assert binding is not None
        request = PluginExecutionRequest(
            action="poll_subscriptions",
            config=json.loads(binding.config_json or "{}"),
            payload={},
            context=PluginExecutionContext(
                tenant_id=identity["tenant_id"],
                binding_id=identity["binding_id"],
                bot_id=identity["bot_id"],
            ),
        )
        live_room = {
            "provider_status": "live",
            "room_id": "100",
            "nickname": "主播",
            "room_name": "直播标题",
            "room_url": "https://www.douyu.com/100",
            "reply_text": "主播 - 直播标题",
        }
        with patch.object(module, "_fetch_room_status_output", return_value=live_room):
            result = module.execute_scheduled_action(db, request)

        assert result.ok is True
        assert result.output["event_count"] == 1
        jobs = db.scalars(select(TaskJob).order_by(TaskJob.id.asc())).all()
        assert len(jobs) == 1
        payload = json.loads(jobs[0].payload_json)
        assert payload["conversation_id"] == identity["conversation_id"]
        assert payload["payload"]["message_type"] == "text"
        assert "douyu.com/100" in payload["payload"]["content"]
        stored = json.loads(binding.config_json or "{}")
        assert stored["room_states"]["100"]["provider_status"] == "live"
