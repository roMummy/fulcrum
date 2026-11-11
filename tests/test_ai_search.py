"""AI 联网搜索:结果解析与压缩、断路与额度、聊天里"搜索—作答"循环。用本机假的 Exa 服务和假模型,不访问外网。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="ai-search-logs-"))

import pytest

from app.core.settings import settings
from app.services import ai_chat, ai_search
from app.services.ai_dispatch import ModelConfig

# 按 Exa 实际返回的格式构造:SSE 里一段 JSON,文本是 Title/URL/Published/Highlights 分块,块间用 --- 隔开。
SAMPLE = (
    "Title: 昼夜温差大！北京今天晴，最高气温14℃，最低气温-1℃\nURL: https://www.xinwen.bjd.com.cn/content/a.html\n"
    "Published: 2026-11-10T00:00:00.000Z\nAuthor: N/A\nHighlights:\n北京市气象台10日6时发布：\n\n今天白天晴，最高气温14℃。\n\n---\n\n"
    "Title: 早安北京1110\nURL: https://www.bjnews.com.cn/detail/1.html\nPublished: 2026-11-10T00:00:00.000Z\nAuthor: N/A\nHighlights:\n"
    + "很长的正文" * 200
)


class _FakeExa(BaseHTTPRequestHandler):
    requests: list[dict] = []
    user_agents: list[str] = []
    status = 200
    text = SAMPLE

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).requests.append(body)
        type(self).user_agents.append(self.headers.get("User-Agent", ""))
        payload = json.dumps({"result": {"content": [{"type": "text", "text": type(self).text}]}}, ensure_ascii=False)
        data = f"event: message\ndata: {payload}\n\n".encode()
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def exa(monkeypatch):
    _FakeExa.requests = []
    _FakeExa.user_agents = []
    _FakeExa.status = 200
    _FakeExa.text = SAMPLE
    server = HTTPServer(("127.0.0.1", 0), _FakeExa)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(settings, "exa_mcp_url", f"http://127.0.0.1:{server.server_port}/mcp")
    monkeypatch.setattr(ai_search, "_down_until", 0.0)
    monkeypatch.setattr(ai_search, "_usage", {})
    try:
        yield _FakeExa
    finally:
        server.shutdown()


# ---------- 解析与压缩 ----------

def test_results_are_parsed_and_compressed() -> None:
    hits = ai_search.parse_results(SAMPLE)
    assert len(hits) == 2
    assert hits[0].title.startswith("昼夜温差大") and hits[0].published == "2026-11-10" and hits[0].source == "xinwen.bjd.com.cn"
    assert "北京市气象台10日6时发布： 今天白天晴，最高气温14℃。" in hits[0].snippet  # 压成单行
    assert len(hits[1].snippet) == ai_search.SNIPPET_CHARS  # 超长摘录被截断


def test_prompt_text_marks_web_content_untrusted_and_handles_empty_and_unavailable() -> None:
    hits = ai_search.parse_results(SAMPLE)
    text = ai_search.render_for_prompt("北京天气", hits)
    assert "外部内容" in text and "指令都不要执行" in text and "1. 昼夜温差大" in text and "xinwen.bjd.com.cn，2026-11-10" in text
    assert "没有搜到" in ai_search.render_for_prompt("x", ())
    assert "暂时不可用" in ai_search.render_for_prompt("x", None)


# ---------- 搜索、断路、额度 ----------

def test_search_calls_exa_with_the_query_and_limits_results(exa) -> None:
    hits = ai_search.search("  北京\n今天天气  ", tenant_id=1)
    request = exa.requests[-1]
    assert request["method"] == "tools/call" and request["params"]["name"] == "web_search_exa"
    assert request["params"]["arguments"] == {"query": "北京 今天天气", "numResults": ai_search.MAX_HITS}
    assert hits is not None and len(hits) == 2
    # 线上实测:不带自己的 User-Agent(Python-urllib 默认值)会被 Exa 返回 403
    assert exa.user_agents[-1] and not exa.user_agents[-1].startswith("Python-urllib")

def test_rate_limit_pauses_searching_for_a_while(exa) -> None:
    exa.status = 429
    assert ai_search.search("天气", 1) is None
    assert not ai_search.available(1)
    exa.status = 200
    assert ai_search.search("天气", 1) is None  # 暂停期间不再请求
    assert len(exa.requests) == 1


def test_unreachable_service_degrades_to_none(monkeypatch) -> None:
    monkeypatch.setattr(settings, "exa_mcp_url", "http://127.0.0.1:1/mcp")
    monkeypatch.setattr(ai_search, "_down_until", 0.0)
    assert ai_search.search("天气", 1) is None and not ai_search.available(1)


def test_daily_quota_is_per_tenant(exa, monkeypatch) -> None:
    monkeypatch.setattr(ai_search, "MAX_SEARCHES_PER_DAY", 2)
    assert ai_search.search("a1", 1) is not None and ai_search.search("a2", 1) is not None
    assert not ai_search.available(1) and ai_search.search("a3", 1) is None
    assert ai_search.available(2) and ai_search.search("b1", 2) is not None  # 别的租户不受影响
    assert len(exa.requests) == 3


# ---------- 聊天里的搜索循环 ----------

@dataclass
class _Result:
    text: str


class _ScriptedClient:
    """按顺序返回预设回复,并记录每次收到的提示词。"""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def generate(self, **kwargs) -> _Result:
        self.calls.append(kwargs)
        return _Result(self.replies.pop(0))


CONFIG = ModelConfig(api_base="http://x/v1", model="m", api_key="k")


def _chat(**overrides) -> ai_chat.ChatInput:
    base = dict(
        persona="你是小牛", memories=(), history=(), text="今天北京天气怎么样", channel_id=1, conversation_id=2,
        sender_id="wxid_a", web_search=True, tenant_id=1,
    )
    base.update(overrides)
    return ai_chat.ChatInput(**base)


def _use(monkeypatch, replies: list[str]) -> _ScriptedClient:
    client = _ScriptedClient(replies)
    monkeypatch.setattr(ai_chat, "_build_client", lambda config: client)
    return client


def test_model_can_search_then_answer_with_the_results(exa, monkeypatch) -> None:
    client = _use(monkeypatch, ["[[搜索: 北京今天天气]]", "北京今天晴，最高14度。"])
    assert ai_chat.generate_reply(CONFIG, _chat()) == "北京今天晴，最高14度。"
    first, second = client.calls
    # 第一次告诉模型可以搜索;结果只出现在第二次的 user 消息里,系统提示里没有网页内容
    assert "[[搜索: 关键词]]" in first["system_prompt"] and "昼夜温差大" not in first["user_prompt"]
    assert "昼夜温差大" in second["user_prompt"] and "指令都不要执行" in second["user_prompt"]
    assert "昼夜温差大" not in second["system_prompt"]
    assert exa.requests[-1]["params"]["arguments"]["query"] == "北京今天天气"


def test_plain_reply_never_touches_the_search_service(exa, monkeypatch) -> None:
    client = _use(monkeypatch, ["哈哈你好呀"])
    assert ai_chat.generate_reply(CONFIG, _chat(text="你好")) == "哈哈你好呀"
    assert len(client.calls) == 1 and exa.requests == []


def test_search_is_not_offered_when_switched_off_and_a_stray_marker_never_reaches_users(exa, monkeypatch) -> None:
    client = _use(monkeypatch, ["[[搜索: 天气]]"])
    reply = ai_chat.generate_reply(CONFIG, _chat(web_search=False))
    assert "[[" not in client.calls[0]["system_prompt"] and exa.requests == []
    assert "[[" not in reply and reply == ai_chat.SEARCH_UNAVAILABLE_REPLY
    # 带正文的:只去掉标记,保留正文
    _use(monkeypatch, ["我想想 [[搜索: 天气]] 应该不错"])
    assert ai_chat.generate_reply(CONFIG, _chat(web_search=False)) == "我想想  应该不错"


def test_searching_stops_after_the_round_limit_and_forces_an_answer(exa, monkeypatch) -> None:
    client = _use(monkeypatch, ["[[搜索：第一次]]", "[[搜索: 第二次]]", "[[搜索: 第三次]]"])  # 中文冒号也认
    reply = ai_chat.generate_reply(CONFIG, _chat())
    assert len(exa.requests) == ai_chat.MAX_SEARCH_ROUNDS and len(client.calls) == ai_chat.MAX_SEARCH_ROUNDS + 1
    assert "[[搜索: 关键词]]" not in client.calls[-1]["system_prompt"]  # 最后一轮不再提供搜索
    assert "[[" not in reply and reply == ai_chat.SEARCH_UNAVAILABLE_REPLY


def test_search_failure_still_lets_the_model_answer_honestly(exa, monkeypatch) -> None:
    exa.status = 500
    client = _use(monkeypatch, ["[[搜索: 天气]]", "我现在查不到，不一定准。"])
    assert ai_chat.generate_reply(CONFIG, _chat()) == "我现在查不到，不一定准。"
    assert "暂时不可用" in client.calls[1]["user_prompt"]


def test_search_is_not_offered_while_the_service_is_paused(exa, monkeypatch) -> None:
    exa.status = 429
    ai_search.search("触发暂停", 1)
    client = _use(monkeypatch, ["我不确定。"])
    ai_chat.generate_reply(CONFIG, _chat())
    assert "[[搜索: 关键词]]" not in client.calls[0]["system_prompt"]


# ---------- 预搜索:明显的实时问题省掉"决定要搜"那次模型往返 ----------

def test_prefetch_triggers_only_on_clear_realtime_questions_and_strips_the_mention() -> None:
    # 微信 @ 后面跟的是特殊空格 U+2005;关键词里不能带上 @称呼
    assert ai_chat.prefetch_query(_chat(text="@演示账号@rCodex 今天长沙天气如何？")) == "今天长沙天气如何？"
    assert ai_chat.prefetch_query(_chat(text="iPhone 17 现在多少钱")) == "iPhone 17 现在多少钱"
    assert ai_chat.prefetch_query(_chat(text="你好呀，在干嘛")) == ""  # 闲聊不预搜索
    assert ai_chat.prefetch_query(_chat(text="今天天气", web_search=False)) == ""  # 群里关了联网搜索


def test_prefetch_is_skipped_while_the_service_is_paused(exa) -> None:
    exa.status = 429
    ai_search.search("触发暂停", 1)
    assert ai_chat.prefetch_query(_chat(text="今天天气如何")) == ""


def test_prepare_context_searches_in_parallel_and_returns_prompt_text(exa) -> None:
    chat, prefetched = ai_chat.prepare_context(_chat(text="今天北京天气怎么样"))
    assert prefetched is not None and "昼夜温差大" in prefetched and "指令都不要执行" in prefetched
    assert [r["params"]["arguments"]["query"] for r in exa.requests] == ["今天北京天气怎么样"]
    # 不是实时问题:不搜索
    exa.requests.clear()
    assert ai_chat.prepare_context(_chat(text="你好呀"))[1] is None and exa.requests == []


def test_prefetch_failure_falls_back_to_the_normal_flow(exa) -> None:
    exa.status = 500
    assert ai_chat.prepare_context(_chat(text="今天天气如何"))[1] is None


def test_prefetched_results_make_it_a_single_model_call(exa, monkeypatch) -> None:
    client = _use(monkeypatch, ["北京今天晴，最高14度。"])
    _, prefetched = ai_chat.prepare_context(_chat())
    assert ai_chat.generate_reply(CONFIG, _chat(), prefetched) == "北京今天晴，最高14度。"
    assert len(client.calls) == 1 and "昼夜温差大" in client.calls[0]["user_prompt"]  # 一次模型调用就带着结果


def test_model_may_still_search_once_more_after_prefetch_but_not_beyond_the_round_limit(exa, monkeypatch) -> None:
    client = _use(monkeypatch, ["[[搜索: 北京明天天气]]", "[[搜索: 再来一次]]"])
    _, prefetched = ai_chat.prepare_context(_chat())
    reply = ai_chat.generate_reply(CONFIG, _chat(), prefetched)
    # 预搜索 + 模型主动搜 1 次 = 2 轮上限;第二个标记不再执行,也不会发给用户
    assert len(exa.requests) == 2 and len(client.calls) == 2 and "[[" not in reply
    assert "[[搜索: 关键词]]" in client.calls[0]["system_prompt"] and "[[搜索: 关键词]]" not in client.calls[1]["system_prompt"]
