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


def _seed(db):
    """建最小数据：租户、机器人、微桥渠道、已启用的 message_sign、已配置的 AI。"""

    from app.models.entities import Bot, BotPlugin, Channel, Plugin, TenantAiSetting
    from app.models.saas import Tenant

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
    return channel


EVENT = {
    "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
    "message": {"content": "我要签到", "sender_id": "wxid_a", "message_type": "text"},
    "details": {"mentioned_self": True},
}


def test_dispatch_only_enqueues_intent_job_and_never_calls_model(monkeypatch) -> None:
    """调度主链不能同步调用模型：规则无命中时只入队一个意图任务，且同一消息不重复入队。"""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base
    from app.models import saas  # noqa: F401
    from app.models.entities import TaskJob
    from app.services import plugin_directory as pd

    called = []
    monkeypatch.setattr(ai_dispatch.PluginTextGenerationClient, "generate", lambda self, **kw: called.append(1))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        channel = _seed(db)
        # 裸 SQLite 会话没有租户自动补全：入队前补 tenant_id。
        from sqlalchemy import event as sa_event
        sa_event.listen(db, "before_flush", lambda s, c, i: [setattr(o, "tenant_id", 1) for o in s.new if hasattr(o, "tenant_id") and o.tenant_id is None])

        plan = pd.build_static_dispatch_plan(db, channel, EVENT)
        assert pd._enqueue_ai_intent(db, plan, EVENT, raw_event_id=5, conversation_id=2, message_id=3) is True
        assert pd._enqueue_ai_intent(db, plan, EVENT, raw_event_id=5, conversation_id=2, message_id=3) is False
        db.flush()
        jobs = db.query(TaskJob).all()
        assert [j.job_type for j in jobs] == ["ai_intent"] and jobs[0].max_attempts == 1
    assert called == []


def test_ai_rewritten_event_is_never_analyzed_again() -> None:
    """AI 改写出的命令若仍不命中，不能再次进入 AI，避免循环调用模型。"""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base
    from app.models import saas  # noqa: F401
    from app.services import plugin_directory as pd

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        channel = _seed(db)
        event = {**EVENT, "details": {"mentioned_self": True, "ai_rewritten": True}}
        plan = pd.build_static_dispatch_plan(db, channel, event)
        assert pd._enqueue_ai_intent(db, plan, event, raw_event_id=5, conversation_id=2, message_id=3) is False


def test_intent_worker_executes_plugin_directly_without_requeue(monkeypatch) -> None:
    """识别出插件后直接执行：记录决策，调用调度链一次，且不再入队新任务。"""

    from sqlalchemy import create_engine
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base
    from app.models import saas  # noqa: F401
    from app.models.entities import AiDecision, TaskJob
    from app.services import ai_intent_worker, plugin_directory

    seen: list[dict] = []
    monkeypatch.setattr(plugin_directory, "build_dispatch_report", lambda db, channel, event, **kw: seen.append(event))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        _seed(db)
        sa_event.listen(db, "before_flush", lambda s, c, i: [setattr(o, "tenant_id", 1) for o in s.new if hasattr(o, "tenant_id") and o.tenant_id is None])
        payload = {"channel_id": 1, "raw_event_id": 5, "conversation_id": 2, "message_id": 3, "normalized_event": EVENT}
        intent = ai_dispatch.AiIntent(status="routed", plugin_code="message_sign", command_text="/签到", confidence=95)
        result = ai_intent_worker._deliver(db, payload, intent)
        assert result["command"] == "/签到"
        # 直接执行：调度链拿到改写后的命令，带防循环标记；没有新入队任何任务。
        assert seen[0]["message"]["content"] == "/签到" and seen[0]["details"]["ai_rewritten"] is True
        assert db.query(TaskJob).count() == 0
        assert db.query(AiDecision).one().status == "routed"


def test_intent_worker_slots_limit_concurrency() -> None:
    """并发名额有上限：占满后不再领取新任务（背压），释放后才可继续。"""

    from app.services import ai_intent_worker as w

    taken = [w._try_reserve_slot() for _ in range(w.MAX_CONCURRENT_INTENTS + 2)]
    assert taken.count(True) == w.MAX_CONCURRENT_INTENTS and taken[-1] is False
    for _ in range(w.MAX_CONCURRENT_INTENTS):
        w._release_slot()
    assert w._try_reserve_slot() is True
    w._release_slot()


def test_text_generation_read_timeout_follows_total_timeout() -> None:
    """非流式推理模型要等十几秒才返回：单次读超时必须跟随总时限，不能被写死成 10 秒。"""

    from app.services.plugin_runtime.http_client import PluginHttpResponse
    from app.services.plugin_runtime.text_generation import PluginTextGenerationClient

    seen: dict = {}

    class FakeHttp:
        def post_json(self, url, body, **kwargs):
            seen.update(kwargs)
            return PluginHttpResponse(
                status_code=200, headers={}, body=b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":3}}', url="https://x/v1/chat/completions",
            )

    client = PluginTextGenerationClient(api_base="https://x/v1", model="m", http_client=FakeHttp())
    client.generate(user_prompt="hi", timeout_seconds=45)
    assert seen["connect_timeout_seconds"] == 45 and seen["total_timeout_seconds"] == 45


def test_slow_model_does_not_block_poller(monkeypatch) -> None:
    """慢模型（2 秒）只占用线程池线程：领取函数立即返回，主 worker 不会被拖住。"""

    import threading
    import time
    from types import SimpleNamespace

    from app.services import ai_intent_worker as w

    release = threading.Event()
    started = threading.Event()

    def slow_run(job_id: int, tenant_id: int) -> None:
        started.set()
        release.wait(5)

    monkeypatch.setattr(w, "_run_intent_job", slow_run)
    monkeypatch.setattr(w, "platform_session", lambda factory: __import__("contextlib").nullcontext(object()))
    claims = iter([SimpleNamespace(id=1, tenant_id=1), None])
    monkeypatch.setattr(w, "claim_next_job", lambda db, job_types: next(claims))

    begin = time.monotonic()
    assert w.poll_ai_intent_jobs() is True
    elapsed = time.monotonic() - begin
    assert started.wait(2) and elapsed < 0.5, f"领取被阻塞了 {elapsed:.2f}s"
    release.set()
    deadline = time.monotonic() + 3
    while w._in_flight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert w._in_flight == 0


def test_effective_content_is_memory_only_and_never_persisted() -> None:
    """AI 改写的命令只存在于会话内存：插件能读到命令，但 flush/commit 后库里仍是用户原话。"""

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base
    from app.models import saas  # noqa: F401
    from app.models.entities import Message

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    raw = lambda db: db.execute(text("select content from messages where id=1")).scalar()  # noqa: E731
    with sessionmaker(bind=engine)() as db:
        db.execute(text(
            "insert into messages (id, tenant_id, conversation_id, direction, content, message_type, status, created_at, updated_at) "
            "values (1, 1, 1, 'in', '@bot 我要签到', 'text', 'received', '2026-11-09', '2026-11-09')"
        ))
        db.commit()
        with ai_dispatch.effective_message_content(db, 1, "/签到"):
            # 插件按消息 ID 回库读取，拿到的是改写后的命令。
            assert db.get(Message, 1).content == "/签到"
            # 即使中途 flush（插件执行里的查询会触发自动刷新），也不会产生 UPDATE。
            db.flush()
            assert raw(db) == "@bot 我要签到"
            assert db.get(Message, 1) not in db.dirty
        assert db.get(Message, 1).content == "@bot 我要签到"
        db.commit()
        assert raw(db) == "@bot 我要签到"
        # 没有改写时是空操作。
        with ai_dispatch.effective_message_content(db, 1, None):
            assert db.get(Message, 1).content == "@bot 我要签到"
