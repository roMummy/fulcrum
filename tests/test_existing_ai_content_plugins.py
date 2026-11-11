from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
from app.core.time import SHANGHAI_TZ
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Plugin, PluginStateRecord, TaskJob
from app.models.saas import Tenant
from app.services.plugin_directory import (
    _extract_command_args,
    _extract_command_name,
    get_discovered_plugin,
    load_directory_plugin,
)
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpResponse
from app.services.plugin_runtime.messages import PluginMessageBatch, PluginMessageRecord


def test_message_summary_uses_group_date_commands_and_clean_runtime_config() -> None:
    """群总结只声明当前两个命令，并由平台统一识别斜杠和井号前缀。"""

    discovered = get_discovered_plugin("message_summary", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == ("总结", "summary")
    assert discovered.capability.chat_types == ("group",)
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config["api_base"] == ""
    assert discovered.runtime_config["min_messages"] == 100
    assert "app_mode" not in discovered.runtime_config
    assert "workflow_input_key" not in discovered.runtime_config
    assert "workflow_session_key" not in discovered.runtime_config

    assert _extract_command_name("#总结 2026-08-30", "/") == "总结"
    assert _extract_command_args("#总结 2026-08-30", "/", "总结") == "2026-08-30"

    loaded = load_directory_plugin("message_summary", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    group_context = type(
        "GroupContext",
        (),
        {"chat_type": "group", "message_type": "text", "content": "#总结 2026-08-30"},
    )()
    direct_context = type(
        "DirectContext",
        (),
        {"chat_type": "direct", "message_type": "text", "content": "/summary"},
    )()
    assert module.matches_event(group_context)["matched"] is True
    assert module.matches_event(direct_context)["matched"] is False

    source = (PROJECT_ROOT / "plugins/message_summary/plugin.py").read_text(encoding="utf-8")
    assert "SessionLocal" not in source
    assert "urlrequest" not in source
    assert "workflow_input_key" not in source
    assert "workflow_session_key" not in source
    assert "192.168.1.240" not in source


def test_message_summary_reads_fixed_day_and_uses_single_dify_text_output() -> None:
    """指定日期总结使用平台消息批次和固定 Dify workflow 契约。"""

    loaded = load_directory_plugin("message_summary", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    current = datetime(2026, 8, 31, 12, 0, tzinfo=SHANGHAI_TZ)
    messages = tuple(
        PluginMessageRecord(
            message_id=index + 1,
            created_at=current - timedelta(hours=20) + timedelta(minutes=index),
            direction="in",
            sender_id=f"member-{index % 4}",
            sender_name=f"成员{index % 4}",
            content=f"第{index + 1}条业务消息",
            formatted_text=f"成员{index % 4}: 第{index + 1}条业务消息",
        )
        for index in range(100)
    )
    batch = PluginMessageBatch(
        conversation_id=50,
        conversation_title="产品研发群",
        channel_type="telegram",
        start_at=datetime(2026, 8, 30, 0, 0, tzinfo=SHANGHAI_TZ),
        end_at=datetime(2026, 8, 31, 0, 0, tzinfo=SHANGHAI_TZ),
        messages=messages,
    )
    captured: dict[str, object] = {}

    class FakeReader:
        def __init__(self, db, context):
            captured["reader_db"] = db
            captured["reader_context"] = context

        def read(self, **kwargs):
            captured["reader_kwargs"] = kwargs
            return batch

    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "data": {
                    "outputs": {"text": "# 群聊摘要\n\n- 已确认主流程\n- 明确下一步负责人"},
                    "total_tokens": 456,
                },
                "trace_id": "provider-sensitive-trace",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        url="https://dify.example.test/v1/workflows/run",
    )

    class FakeClient:
        def post_json(self, url, payload, **kwargs):
            captured.update({"url": url, "payload": payload, "http_kwargs": kwargs})
            return response

    request = PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "api_base": "https://dify.example.test/v1",
            "api_key": "tenant-summary-key",
            "min_messages": 100,
            "max_messages": 1000,
            "render_as_image": False,
        },
        payload={"content": "#总结 2026-08-30", "command_name": "总结"},
        context=PluginExecutionContext(
            tenant_id=1,
            binding_id=2,
            bot_id=3,
            channel_id=4,
            conversation_id=50,
            message_id=60,
            sender_id="member-operator",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    expected_db = object()
    with (
        patch.object(module, "now_shanghai", return_value=current),
        patch.object(module, "PluginMessageReader", FakeReader),
        patch.object(module, "PluginHttpClient", return_value=FakeClient()),
    ):
        result = module.execute_message_action(expected_db, request)

    assert result.ok is True
    assert result.output["provider_status"] == "completed"
    assert result.output["message_count"] == 100
    assert result.output["participant_count"] == 4
    assert result.output["period_label"] == "2026-08-30"
    assert result.output["token_usage"] == 456
    assert result.output["reply_type"] == "text"
    assert result.output["replies"] == [
        {"type": "text", "content": "# 群聊摘要\n\n- 已确认主流程\n- 明确下一步负责人"}
    ]
    assert captured["reader_db"] is expected_db
    reader_kwargs = captured["reader_kwargs"]
    assert reader_kwargs["start_at"] == datetime(2026, 8, 30, 0, 0, tzinfo=SHANGHAI_TZ)
    assert reader_kwargs["end_at"] == datetime(2026, 8, 31, 0, 0, tzinfo=SHANGHAI_TZ)
    assert captured["url"] == "https://dify.example.test/v1/workflows/run"
    request_body = captured["payload"]
    assert set(request_body["inputs"]) == {"query", "group_name", "chat_content", "summary_mode"}
    assert request_body["inputs"]["summary_mode"] == "markdown"
    assert "第100条业务消息" in request_body["inputs"]["chat_content"]
    serialized_output = json.dumps(result.output, ensure_ascii=False)
    assert "第100条业务消息" not in serialized_output
    assert "tenant-summary-key" not in serialized_output
    assert "provider-sensitive-trace" not in serialized_output


def test_message_summary_defaults_to_yesterday_and_skips_small_groups() -> None:
    """未指定日期时读取昨天零点到现在，低于 100 条不调用 Dify。"""

    loaded = load_directory_plugin("message_summary", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    current = datetime(2026, 8, 31, 12, 30, tzinfo=SHANGHAI_TZ)
    captured: dict[str, object] = {}

    class FakeReader:
        def __init__(self, db, context):
            del db, context

        def read(self, **kwargs):
            captured.update(kwargs)
            return PluginMessageBatch(
                conversation_id=50,
                conversation_title="安静群",
                channel_type="telegram",
                start_at=kwargs["start_at"],
                end_at=kwargs["end_at"],
                messages=(),
            )

    class ForbiddenClient:
        def post_json(self, *args, **kwargs):
            raise AssertionError("消息不足时不应调用 Dify")

    request = PluginExecutionRequest(
        action="execute",
        config={
            "api_base": "https://dify.example.test/v1",
            "api_key": "tenant-key",
            "render_as_image": False,
        },
        payload={"content": "/summary"},
        context=PluginExecutionContext(
            tenant_id=1,
            binding_id=2,
            bot_id=3,
            channel_id=4,
            conversation_id=50,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    with (
        patch.object(module, "now_shanghai", return_value=current),
        patch.object(module, "PluginMessageReader", FakeReader),
        patch.object(module, "PluginHttpClient", return_value=ForbiddenClient()),
    ):
        result = module.execute_message_action(object(), request)

    assert result.ok is True
    assert result.output["provider_status"] == "insufficient_messages"
    assert result.output["message_count"] == 0
    assert captured["start_at"] == datetime(2026, 8, 30, 0, 0, tzinfo=SHANGHAI_TZ)
    assert captured["end_at"] == current


def test_message_summary_prepare_cache_is_minimal_and_push_reuses_it() -> None:
    """凌晨只缓存最小摘要，早上命中缓存后直接创建群聊出站任务。"""

    loaded = load_directory_plugin("message_summary", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    current = datetime(2026, 8, 31, 9, 0, tzinfo=SHANGHAI_TZ)
    binding = SimpleNamespace(
        id=2,
        tenant_id=1,
        bot_id=3,
        status="enabled",
        bot=SimpleNamespace(is_enabled=True),
    )
    conversation = SimpleNamespace(
        id=50,
        bot_id=3,
        channel_id=4,
        chat_type="group",
        external_id="room-50",
        title="产品研发群",
    )

    class FakeDb:
        def get(self, model, object_id):
            if model is BotPlugin and int(object_id) == 2:
                return binding
            if model is Conversation and int(object_id) == 50:
                return conversation
            return None

    cache: dict[str, dict[str, object]] = {}

    class FakeStore:
        def __init__(self, db, context):
            del db, context

        def get(self, namespace, state_key, **kwargs):
            del namespace, kwargs
            return cache.get(state_key)

        def put(self, namespace, state_key, payload, **kwargs):
            del namespace, kwargs
            cache[state_key] = dict(payload)

    generated_calls = []

    def fake_generate(db, request):
        del db
        generated_calls.append(request)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="generated",
            output={
                "provider_status": "completed",
                "summary": "# 昨日摘要\n\n- 主流程已确认",
                "message_count": 120,
                "participant_count": 8,
                "period_start": "2026-08-30T00:00:00+08:00",
                "period_end": "2026-08-31T00:00:00+08:00",
                "date_key": "2026-08-30",
                "token_usage": 300,
                "replies": [{"type": "text", "content": "不应缓存的回复"}],
                "messages": ["不应缓存的消息正文"],
            },
        )

    outbound_capture: dict[str, object] = {}

    def fake_enqueue(db, **kwargs):
        del db
        target = list(kwargs["conversations"])[0]
        outbound_capture["payloads"] = kwargs["payload_builder"](target, SimpleNamespace())
        return {"job_ids": [901], "failed_targets": [], "pushed_conversation_count": 1}

    context = PluginExecutionContext(
        tenant_id=1,
        binding_id=2,
        bot_id=3,
        channel_id=4,
        conversation_id=50,
        channel_type="telegram",
        chat_type="group",
    )
    base_config = {
        "api_base": "https://dify.example.test/v1",
        "api_key": "tenant-key",
        "render_as_image": False,
    }
    with (
        patch.object(module, "now_shanghai", return_value=current),
        patch.object(module, "PluginStateStore", FakeStore),
        patch.object(module, "execute_message_action", side_effect=fake_generate),
        patch.object(module, "enqueue_outbound_jobs_by_conversation", side_effect=fake_enqueue),
    ):
        prepare = module.execute_scheduled_action(
            FakeDb(),
            PluginExecutionRequest(
                action="daily_summary_prepare",
                config=base_config,
                payload={},
                context=context,
            ),
        )
        push = module.execute_scheduled_action(
            FakeDb(),
            PluginExecutionRequest(
                action="daily_summary",
                config=base_config,
                payload={},
                context=context,
            ),
        )

    assert prepare.ok is True
    assert prepare.output["cache_status"] == "prepared"
    assert push.ok is True
    assert push.output["cache_hit"] is True
    assert push.output["outbound_job_ids"] == [901]
    assert len(generated_calls) == 1
    stored = next(iter(cache.values()))
    assert set(stored) == {
        "summary",
        "message_count",
        "participant_count",
        "period_start",
        "period_end",
        "date_key",
        "token_usage",
    }
    assert "tenant-key" not in json.dumps(stored, ensure_ascii=False)
    assert "不应缓存的消息正文" not in json.dumps(stored, ensure_ascii=False)
    assert outbound_capture["payloads"] == [
        {
            "external_id": "room-50",
            "receiver_id": "room-50",
            "chat_type": "group",
            "message_type": "text",
            "content": "# 昨日摘要\n\n- 主流程已确认",
        }
    ]


def test_global_news_matches_latest_commands_and_uses_clean_tenant_config() -> None:
    """全球新闻补齐来源最新命令和 Telegram，并删除固定 Dify/Authorization 配置。"""

    discovered = get_discovered_plugin("global_news", force=True)
    assert discovered is not None
    assert discovered.matcher.commands == (
        "全球新闻",
        "国际新闻",
        "环球新闻",
        "政经新闻",
        "政治经济新闻",
    )
    assert "telegram" in discovered.capability.channel_types
    assert discovered.runtime_config["llm_api_base"] == ""
    assert discovered.runtime_config["llm_model"] == ""
    assert "dify_api_url" not in discovered.runtime_config
    assert "authorization" not in discovered.runtime_config

    loaded = load_directory_plugin("global_news", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    group_context = type(
        "GroupContext",
        (),
        {"chat_type": "group", "message_type": "text", "content": "#政治经济新闻"},
    )()
    assert module.matches_event(group_context)["matched"] is True

    source = (PROJECT_ROOT / "plugins/global_news/plugin.py").read_text(encoding="utf-8")
    assert "urlrequest" not in source
    assert "UnifiedLLMClient" not in source
    assert "WechatAPIClient" not in source
    assert "threading.Thread" not in source
    assert "192.168.1.240" not in source


def test_global_news_parses_rss_and_atom_with_structured_xml() -> None:
    """固定新闻源同时支持 RSS item 和 Atom entry，不使用字符串切割。"""

    loaded = load_directory_plugin("global_news", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    rss = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/rss+xml"},
        body=(
            "<rss><channel><item><title> RSS  Headline </title>"
            "<link>https://news.example/rss</link><pubDate>Mon</pubDate></item></channel></rss>"
        ).encode("utf-8"),
        url="https://news.example/rss.xml",
    )
    atom = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/atom+xml"},
        body=(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom Headline</title>'
            '<link href="https://news.example/atom"/><updated>2026-08-31</updated></entry></feed>'
        ).encode("utf-8"),
        url="https://news.example/atom.xml",
    )

    rss_items = module._parse_feed_items("bbc", rss, limit=5)
    atom_items = module._parse_feed_items("cnn", atom, limit=5)
    assert rss_items[0].title == "RSS Headline"
    assert rss_items[0].link == "https://news.example/rss"
    assert atom_items[0].title == "Atom Headline"
    assert atom_items[0].link == "https://news.example/atom"


def test_global_news_uses_generic_text_generation_and_drops_full_headlines() -> None:
    """新闻分析复用通用模型接口，执行结果不保存标题列表、密钥或上游 trace。"""

    loaded = load_directory_plugin("global_news", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    fetched = module.NewsFetchResult(
        items=(
            module.NewsItem("bbc", "Global market update", "https://news.example/1", "2026-08-31"),
            module.NewsItem("cnn", "Election policy update", "https://news.example/2", "2026-08-31"),
        ),
        successful_sources=("bbc", "cnn"),
        failed_sources=("fox",),
    )
    captured: dict[str, object] = {}

    class FakeGenerator:
        def __init__(self, **kwargs):
            captured["client_config"] = kwargs

        def generate(self, **kwargs):
            captured["generate_args"] = kwargs
            return SimpleNamespace(text="# 全球简报\n\n- 市场与政策风险上升", token_usage=234)

    request = PluginExecutionRequest(
        action="execute",
        config={
            "enabled": True,
            "sources": ["bbc", "cnn", "fox"],
            "llm_api_base": "https://llm.example.test/v1",
            "llm_api_key": "tenant-news-key",
            "llm_model": "tenant-news-model",
            "render_as_image": False,
        },
        payload={"content": "全球新闻"},
        context=PluginExecutionContext(
            tenant_id=1,
            binding_id=2,
            bot_id=3,
            channel_id=4,
            conversation_id=5,
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    with (
        patch.object(module, "_fetch_sources", return_value=fetched),
        patch.object(module, "PluginTextGenerationClient", FakeGenerator),
    ):
        result = module.execute_action(request)

    assert result.ok is True
    assert result.output == {
        "provider_status": "completed",
        "headline_count": 2,
        "successful_source_count": 2,
        "failed_source_count": 1,
        "successful_sources": ["bbc", "cnn"],
        "failed_sources": ["fox"],
        "summary": "# 全球简报\n\n- 市场与政策风险上升",
        "token_usage": 234,
        "reply_type": "text",
        "replies": [{"type": "text", "content": "# 全球简报\n\n- 市场与政策风险上升"}],
    }
    assert captured["client_config"] == {
        "api_base": "https://llm.example.test/v1",
        "model": "tenant-news-model",
        "api_key": "tenant-news-key",
    }
    assert "Global market update" in captured["generate_args"]["user_prompt"]
    serialized = json.dumps(result.output, ensure_ascii=False)
    assert "Global market update" not in serialized
    assert "tenant-news-key" not in serialized
    assert "trace" not in serialized.lower()


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
        db.add(conversation)
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "room_subscriptions": {str(conversation.id): ["100"]},
                    "highlight_enabled": False,
                    "poll_interval_seconds": 60,
                }
            ),
        )
        db.add(binding)
        db.flush()
        db.add(
            PluginStateRecord(
                tenant_id=tenant.id,
                bot_plugin_id=binding.id,
                namespace="douyu_room",
                state_key="100",
                payload_json=json.dumps({"provider_status": "offline"}, ensure_ascii=False),
            )
        )
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
        assert "room_states" not in stored
        room_state = db.scalar(
            select(PluginStateRecord).where(
                PluginStateRecord.bot_plugin_id == identity["binding_id"],
                PluginStateRecord.namespace == "douyu_room",
                PluginStateRecord.state_key == "100",
            )
        )
        assert room_state is not None
        assert json.loads(room_state.payload_json)["provider_status"] == "live"
