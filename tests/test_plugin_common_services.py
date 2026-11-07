from __future__ import annotations

import base64
import os
import socket
from datetime import timedelta
from pathlib import Path
from urllib.request import Request

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-common-services"

# HTTP 客户端模块初始化日志，测试必须在导入 app 前使用独立目录。
os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, BotPlugin, Plugin, PluginStateRecord
from app.models.saas import Tenant
from app.services.plugin_directory import _build_auto_outbound_payloads
from app.services.plugin_runtime.contracts import PluginExecutionContext
from app.services.plugin_runtime.http_client import (
    PluginHttpClient,
    PluginHttpResponse,
    PluginHttpRedirectError,
    PluginHttpResponseTooLarge,
    PluginHttpTimeoutError,
    _LimitedRedirectHandler,
    redact_url,
    sanitize_http_headers,
)
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.plugin_runtime.replies import (
    PluginReplyError,
    file_reply,
    image_reply,
    link_reply,
    standard_replies,
    text_reply,
    video_reply,
)
from app.services.plugin_runtime.state_store import PluginStateError, PluginStateStore
from app.services.plugin_runtime.text_generation import (
    PluginTextGenerationClient,
    PluginTextGenerationError,
)


@pytest.fixture()
def common_service_fixture(tmp_path: Path):
    """建立两个租户和两个插件绑定，验证相同状态键不会跨租户冲突。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'plugin-common.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        plugin = Plugin(code="common-test", display_name="通用能力测试", plugin_type="message")
        db.add_all([alpha, beta, plugin])
        db.flush()
        alpha_bot = Bot(tenant_id=alpha.id, name="甲机器人", status="running", is_enabled=True)
        beta_bot = Bot(tenant_id=beta.id, name="乙机器人", status="running", is_enabled=True)
        db.add_all([alpha_bot, beta_bot])
        db.flush()
        alpha_binding = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        beta_binding = BotPlugin(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add_all([alpha_binding, beta_binding])
        db.commit()
        identity = {
            "alpha_tenant_id": int(alpha.id),
            "alpha_bot_id": int(alpha_bot.id),
            "alpha_binding_id": int(alpha_binding.id),
            "beta_tenant_id": int(beta.id),
            "beta_bot_id": int(beta_bot.id),
            "beta_binding_id": int(beta_binding.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _context(identity: dict[str, int], prefix: str) -> PluginExecutionContext:
    return PluginExecutionContext(
        tenant_id=identity[f"{prefix}_tenant_id"],
        binding_id=identity[f"{prefix}_binding_id"],
        bot_id=identity[f"{prefix}_bot_id"],
    )


def test_text_generation_client_uses_openai_compatible_contract_without_raw_response() -> None:
    """通用文本生成只返回文本和 token，不向插件暴露供应商 trace。"""

    captured: dict[str, object] = {}
    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=(
            '{"choices":[{"message":{"role":"assistant","content":"生成结果"}}],'
            '"usage":{"total_tokens":321},"trace_id":"provider-sensitive-trace"}'
        ).encode("utf-8"),
        url="https://llm.example.test/v1/chat/completions",
    )

    class FakeHttpClient:
        def post_json(self, url, payload, **kwargs):
            captured.update({"url": url, "payload": payload, "kwargs": kwargs})
            return response

    client = PluginTextGenerationClient(
        api_base="https://llm.example.test/v1",
        model="tenant-model",
        api_key="tenant-secret",
        http_client=FakeHttpClient(),
    )
    result = client.generate(
        system_prompt="只输出结论",
        user_prompt="请总结新闻",
        user_id="tenant-1:binding-2",
        timeout_seconds=30,
    )

    assert result.text == "生成结果"
    assert result.token_usage == 321
    assert captured["url"] == "https://llm.example.test/v1/chat/completions"
    assert captured["payload"]["model"] == "tenant-model"
    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "只输出结论"},
        {"role": "user", "content": "请总结新闻"},
    ]
    assert captured["kwargs"]["headers"] == {"Authorization": "Bearer tenant-secret"}
    assert "provider-sensitive-trace" not in repr(result)


def test_text_generation_client_rejects_nonstandard_response() -> None:
    """缺少标准 choices.message.content 时直接失败，不解析供应商私有字段。"""

    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=b'{"answer":"private-provider-shape"}',
        url="https://llm.example.test/v1/chat/completions",
    )
    fake_http = type("FakeHttp", (), {"post_json": lambda self, *args, **kwargs: response})()
    client = PluginTextGenerationClient(
        api_base="https://llm.example.test/v1",
        model="tenant-model",
        http_client=fake_http,
    )
    with pytest.raises(PluginTextGenerationError, match="choices"):
        client.generate(user_prompt="test")


def test_plugin_state_store_isolates_tenants_and_expires_records(common_service_fixture) -> None:
    """相同命名空间和键在不同租户独立保存，过期后立即不可读。"""

    factory, identity = common_service_fixture
    current = now_shanghai()
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        store = PluginStateStore(db, _context(identity, "alpha"))
        store.put("conversation.cache", "same-key", {"value": "alpha"}, ttl_seconds=60, now=current)
        db.commit()

    with tenant_scope(identity["beta_tenant_id"]), tenant_session(factory) as db:
        store = PluginStateStore(db, _context(identity, "beta"))
        store.put("conversation.cache", "same-key", {"value": "beta"}, ttl_seconds=1, now=current)
        db.commit()
        assert store.get("conversation.cache", "same-key", now=current) == {"value": "beta"}
        assert store.get("conversation.cache", "same-key", now=current + timedelta(seconds=2)) is None
        db.commit()

    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        store = PluginStateStore(db, _context(identity, "alpha"))
        assert store.get("conversation.cache", "same-key", now=current) == {"value": "alpha"}

    with platform_session(factory) as db:
        rows = db.scalars(select(PluginStateRecord).order_by(PluginStateRecord.tenant_id.asc())).all()
        assert [(row.tenant_id, row.payload_json) for row in rows] == [
            (identity["alpha_tenant_id"], '{"value":"alpha"}')
        ]


def test_plugin_state_store_rejects_invalid_or_oversized_payload(common_service_fixture) -> None:
    """状态键和内容必须保持可维护且不超过小型状态容量。"""

    factory, identity = common_service_fixture
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        store = PluginStateStore(db, _context(identity, "alpha"))
        with pytest.raises(PluginStateError):
            store.put("bad namespace", "key", {"ok": True})
        with pytest.raises(PluginStateError):
            store.put("cache", "large", {"value": "x" * (64 * 1024)})


def test_plugin_rate_limiter_uses_independent_fixed_windows(common_service_fixture) -> None:
    """同一绑定的用户和群分桶互不影响，超限后返回明确重试时间。"""

    factory, identity = common_service_fixture
    current = now_shanghai()
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        limiter = PluginRateLimiter(db, _context(identity, "alpha"))
        first = limiter.consume("user", identity="member-1", limit=2, window_seconds=60, now=current)
        second = limiter.consume("user", identity="member-1", limit=2, window_seconds=60, now=current)
        blocked = limiter.consume("user", identity="member-1", limit=2, window_seconds=60, now=current)
        other_group = limiter.consume("group", identity="room-1", limit=1, window_seconds=60, now=current)
        reset = limiter.consume(
            "user",
            identity="member-1",
            limit=2,
            window_seconds=60,
            now=current + timedelta(seconds=61),
        )
        db.commit()

    assert first.allowed is True and first.remaining == 1
    assert second.allowed is True and second.remaining == 0
    assert blocked.allowed is False and blocked.retry_after_seconds == 60
    assert other_group.allowed is True
    assert reset.allowed is True and reset.remaining == 1


class _FakeResponse:
    """模拟 urllib 响应，定向验证读取上限和 JSON 解码。"""

    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = headers or {"Content-Type": "application/json; charset=utf-8"}

    def read(self, size: int) -> bytes:
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def geturl(self) -> str:
        return "https://api.example.test/result"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
        return False


class _FakeOpener:
    def __init__(self, response: _FakeResponse | None = None, *, timeout_error: bool = False):
        self.response = response
        self.timeout_error = timeout_error
        self.calls = 0

    def open(self, request: Request, timeout: float):  # noqa: ARG002
        self.calls += 1
        if self.timeout_error:
            raise socket.timeout("timeout")
        assert self.response is not None
        return self.response


def test_plugin_http_client_limits_body_retries_and_redacts_secrets() -> None:
    """HTTP 客户端限制响应体，只重试幂等请求，并在日志字段中脱敏。"""

    success_opener = _FakeOpener(_FakeResponse(b'{"ok":true}'))
    client = PluginHttpClient(opener_factory=lambda _limit: success_opener)
    response = client.request(
        "GET",
        "https://api.example.test/data?api_key=secret&city=shanghai",
        headers={"Authorization": "Bearer secret", "X-Trace": "trace-1"},
    )
    assert response.json() == {"ok": True}
    assert sanitize_http_headers({"Authorization": "secret", "X-Trace": "trace-1"}) == {
        "Authorization": "[REDACTED]",
        "X-Trace": "trace-1",
    }
    assert "secret" not in redact_url("https://api.example.test/data?api_key=secret&city=shanghai")

    large_opener = _FakeOpener(
        _FakeResponse(b"x" * 20, headers={"Content-Length": "20"})
    )
    with pytest.raises(PluginHttpResponseTooLarge):
        PluginHttpClient(opener_factory=lambda _limit: large_opener).request(
            "GET",
            "https://api.example.test/large",
            max_response_bytes=10,
        )

    timeout_get = _FakeOpener(timeout_error=True)
    with pytest.raises(PluginHttpTimeoutError):
        PluginHttpClient(opener_factory=lambda _limit: timeout_get).request(
            "GET",
            "https://api.example.test/timeout",
            retry_count=1,
        )
    assert timeout_get.calls == 2

    timeout_post = _FakeOpener(timeout_error=True)
    with pytest.raises(PluginHttpTimeoutError):
        PluginHttpClient(opener_factory=lambda _limit: timeout_post).request(
            "POST",
            "https://api.example.test/timeout",
            retry_count=2,
        )
    assert timeout_post.calls == 1


def test_plugin_http_redirect_limit_rejects_first_redirect_when_disabled() -> None:
    """重定向上限为零时，首个跳转必须被平台直接拒绝。"""

    handler = _LimitedRedirectHandler(0)
    with pytest.raises(PluginHttpRedirectError):
        handler.redirect_request(
            Request("https://api.example.test/start"),
            None,
            302,
            "Found",
            {},
            "https://api.example.test/next",
        )


def test_standard_replies_validate_type_url_file_scope_and_count(tmp_path: Path) -> None:
    """标准回复只接受平台消息类型，并限制本地文件归属和单次数量。"""

    allowed_root = tmp_path / "tenant-files"
    allowed_root.mkdir()
    image_file = allowed_root / "image.bin"
    image_file.write_bytes(b"image")
    outside_file = tmp_path / "outside.bin"
    outside_file.write_bytes(b"outside")
    image_data = base64.b64encode(b"image").decode("ascii")
    video_data = base64.b64encode(b"video").decode("ascii")

    replies = standard_replies(
        [
            text_reply(" 处理完成 "),
            image_reply(image_path=image_file, allowed_root=allowed_root),
            file_reply(file_name="report.txt", file_url="https://files.example.test/report.txt"),
            video_reply(video_base64=video_data, cover_base64=image_data, play_length=3),
            link_reply(
                title="租户活动",
                description="查看活动详情",
                url="https://tenant.example.test/activity?id=1",
                thumb_url="https://tenant.example.test/activity.png",
            ),
        ],
        allowed_root=allowed_root,
    )
    assert [item["type"] for item in replies] == ["text", "image", "file", "video", "link"]

    with pytest.raises(PluginReplyError):
        image_reply(image_path=outside_file, allowed_root=allowed_root)
    with pytest.raises(PluginReplyError):
        file_reply(file_name="report.txt", file_url="file:///tmp/report.txt")
    with pytest.raises(PluginReplyError):
        link_reply(url="javascript:alert(1)")
    with pytest.raises(PluginReplyError):
        standard_replies([text_reply(str(index)) for index in range(9)])


def test_link_replies_normalize_by_channel_capability() -> None:
    """链接回复保持单一结构，目录层只在渠道确实不支持卡片时降级为文本。"""

    reply = {
        "replies": [
            {
                "type": "link",
                "title": "活动中心",
                "description": "查看本周活动",
                "url": "https://tenant.example.test/activity",
                "thumb_url": "https://tenant.example.test/activity.png",
            }
        ]
    }
    ipad_context = type(
        "FakeContext",
        (),
        {
            "conversation_external_id": "room-link@chatroom",
            "content": "test",
            "channel_type": "wechat_ipad",
        },
    )()
    telegram_context = type(
        "FakeContext",
        (),
        {
            "conversation_external_id": "telegram-room",
            "content": "test",
            "channel_type": "telegram",
        },
    )()

    ipad_payloads = _build_auto_outbound_payloads(ipad_context, reply)
    telegram_payloads = _build_auto_outbound_payloads(telegram_context, reply)

    assert ipad_payloads[0]["message_type"] == "link"
    assert ipad_payloads[0]["link_url"] == "https://tenant.example.test/activity"
    assert telegram_payloads == [
        {
            "message_type": "text",
            "content": "活动中心\n查看本周活动\nhttps://tenant.example.test/activity",
        }
    ]
