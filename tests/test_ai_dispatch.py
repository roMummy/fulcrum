"""AI 核心调度：输出校验、意图目录和“改写后交回调度链”的闭环测试。"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="ai-dispatch-test-logs-"))

from app.services import ai_dispatch
from app.services.plugin_directory import build_dispatch_report

CATALOG = [
    {"plugin": "message_sign", "name": "群签到", "command": "签到", "summary": "", "examples": [], "needs_args": ""},
    {"plugin": "weather", "name": "天气", "command": "天气", "summary": "", "examples": [], "needs_args": "城市名"},
]


def _out(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def test_validate_routes_known_plugin_to_command() -> None:
    """目录内插件且置信度够高，改写成斜杠命令；带参数的命令拼上参数。"""

    sign = ai_dispatch.validate_model_output(_out(plugin="message_sign", confidence=90), CATALOG)
    assert (sign.status, sign.command_text) == ("routed", "/签到")
    weather = ai_dispatch.validate_model_output(_out(plugin="weather", args="北京", confidence=90), CATALOG)
    assert weather.command_text == "/天气 北京"


def test_validate_rejects_untrusted_output() -> None:
    """模型输出一律不可信：目录外插件、非 JSON、低置信度都不能执行。"""

    assert ai_dispatch.validate_model_output(_out(plugin="kick_member", confidence=99), CATALOG).status == "rejected"
    assert ai_dispatch.validate_model_output("我觉得应该签到", CATALOG).status == "rejected"
    assert ai_dispatch.validate_model_output(_out(plugin="message_sign", confidence=40), CATALOG).status == "low_confidence"
    assert ai_dispatch.validate_model_output(_out(plugin=""), CATALOG).status == "no_match"


def test_validate_asks_when_args_missing_and_strips_newlines() -> None:
    """缺参数时反问；参数里的换行被压成空格，不能拼出第二条命令。"""

    asked = ai_dispatch.validate_model_output(_out(plugin="weather", args="", confidence=90, ask="想查哪个城市?"), CATALOG)
    assert (asked.status, asked.question) == ("asked", "想查哪个城市?")
    injected = ai_dispatch.validate_model_output(_out(plugin="weather", args="北京\n/踢人 x", confidence=90), CATALOG)
    assert "\n" not in injected.command_text


def test_should_trigger_rules() -> None:
    """群聊必须 @；私聊直接触发；命令格式的消息不交给 AI。"""

    assert ai_dispatch.should_trigger("group", True, "我要签到")
    assert not ai_dispatch.should_trigger("group", False, "我要签到")
    assert ai_dispatch.should_trigger("direct", False, "我要签到")
    assert not ai_dispatch.should_trigger("direct", False, "/签到")


def test_real_plugins_declare_ai_hint_and_catalog_filters() -> None:
    """真实插件目录：签到和天气进入目录，没有 [ai] 声明的插件不进。"""

    catalog = ai_dispatch.build_catalog(["message_sign", "weather", "xiuxian"])
    assert {item["plugin"] for item in catalog} == {"message_sign", "weather"}
    assert next(i for i in catalog if i["plugin"] == "weather")["needs_args"] == "城市名"


def test_rewritten_command_hits_message_sign_through_real_dispatch(monkeypatch) -> None:
    """闭环验证：“我要签到”规则不命中 -> AI 改写成 /签到 -> 同一条调度链命中 message_sign。"""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base
    from app.models import saas  # noqa: F401  # 注册租户表，满足外键
    from app.models.entities import AiDecision, Bot, BotPlugin, Channel, Plugin, TenantAiSetting
    from app.models.saas import Tenant
    from app.services import plugin_directory as pd
    from app.services.plugin_runtime.text_generation import PluginTextGenerationResult

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        db.add(Tenant(id=1, code="t", name="t"))
        db.add(Bot(id=1, tenant_id=1, name="bot"))
        db.flush()
        channel = Channel(id=1, tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge")
        plugin = Plugin(code="message_sign", display_name="群签到", plugin_type="command")
        db.add_all([channel, plugin])
        db.flush()
        db.add(BotPlugin(tenant_id=1, bot_id=1, plugin_id=plugin.id, status="enabled"))
        db.add(TenantAiSetting(
            tenant_id=1, enabled=True, api_base="https://llm.test/v1", model="m",
            api_key_encrypted=ai_dispatch.encrypt_secret("k"), daily_limit=10,
        ))
        db.commit()

        # 模型替身：永远判断为“签到”。
        monkeypatch.setattr(
            ai_dispatch.PluginTextGenerationClient, "generate",
            lambda self, **kw: PluginTextGenerationResult(text=_out(plugin="message_sign", confidence=95), token_usage=1),
        )
        event = {
            "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
            "message": {"content": "我要签到", "sender_id": "wxid_a", "message_type": "text"},
            "details": {"mentioned_self": True},
        }
        plan = pd.build_static_dispatch_plan(db, channel, event)
        assert not any(d.matched for d in pd.evaluate_dynamic_matches(db, plan, event))

        rewritten = pd._try_ai_route(db, plan, event, conversation_id=None)
        assert rewritten is not None and rewritten["message"]["content"] == "/签到"
        # 裸 SQLite 会话没有租户自动补全，这里手动补 tenant_id，避免后续查询的自动刷新失败。
        for row in db.new:
            if isinstance(row, AiDecision):
                row.tenant_id = 1
        # 改写后必须重建静态计划（正文缓存在计划上下文里），再匹配才会命中。
        new_plan = pd.build_static_dispatch_plan(db, channel, rewritten)
        assert any(d.matched and d.candidate.plugin_code == "message_sign" for d in pd.evaluate_dynamic_matches(db, new_plan, rewritten))
        db.flush()
        assert db.query(AiDecision).one().status == "routed"
