from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-gscore-adapter"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Message, Plugin
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


@pytest.fixture()
def gscore_fixture(tmp_path: Path):
    """建立一条真实租户消息链路，测试适配器不会信任执行载荷中的伪造身份。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'gscore.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="gscore", name="GScore 租户", status="active")
        db.add(tenant)
        db.flush()
        plugin = Plugin(
            code="gscore_adapter",
            display_name="GScore 适配器",
            plugin_type="command",
        )
        db.add(plugin)
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="游戏机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="游戏 Telegram",
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
            external_id="group-real-100",
            title="游戏交流群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        binding = BotPlugin(
            tenant_id=tenant.id,
            bot_id=bot.id,
            plugin_id=plugin.id,
            status="enabled",
        )
        db.add(binding)
        db.flush()
        message = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            direction="in",
            sender_id="member-real-200",
            sender_name="真实成员",
            content="#早柚 查询角色",
            message_type="text",
            external_message_id="message-real-300",
            status="received",
        )
        db.add(message)
        db.commit()
        identity = {
            "tenant_id": int(tenant.id),
            "bot_id": int(bot.id),
            "binding_id": int(binding.id),
            "channel_id": int(channel.id),
            "conversation_id": int(conversation.id),
            "message_id": int(message.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _request(identity: dict[str, int], *, config: dict | None = None) -> PluginExecutionRequest:
    return PluginExecutionRequest(
        action="execute",
        config=config
        or {
           
            "api_url": "https://gscore.example.test/v1/message",
            "api_key": "tenant-gscore-key",
            "request_timeout_seconds": 20,
            "max_replies": 6,
        },
        payload={
            "content": "早柚 伪造查询",
            "tenant_id": 999,
            "bot_id": 999,
            "channel_id": 999,
            "conversation_external_id": "forged-room",
            "sender_id": "forged-user",
        },
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=identity["message_id"],
            sender_id="member-real-200",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )


def test_gscore_adapter_declares_single_business_command_and_tenant_secret() -> None:
    """目录只暴露业务命令和租户自备凭证，不保留重连或特定 Provider 能力。"""

    discovered = get_discovered_plugin("gscore_adapter", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.manifest.plugin_type == "command"
    assert discovered.matcher.commands == ("早柚",)
    assert discovered.secret_fields == ("api_key",)
    assert discovered.runtime_config["api_url"] == ""
    assert "api_key" not in discovered.runtime_config
    assert "telegram" in discovered.capability.channel_types
    assert set(discovered.capability.chat_types) == {"direct", "group"}

    source = (PROJECT_ROOT / "plugins/gscore_adapter/plugin.py").read_text(encoding="utf-8")
    assert "websockets" not in source
    assert "WechatAPIClient" not in source
    assert "GroupBotManager" not in source
    assert "重连早柚" not in source


def test_gscore_adapter_uses_trusted_message_and_returns_standard_replies(
    gscore_fixture,
    monkeypatch,
) -> None:
    """外部请求只使用落库消息，响应目标字段不能改变当前租户和会话。"""

    factory, identity = gscore_fixture
    loaded = load_directory_plugin("gscore_adapter", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    image_data = base64.b64encode(b"gscore-image").decode("ascii")
    response = PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "tenant_id": 999,
                "channel_id": 999,
                "bot_id": "forged-bot",
                "target_id": "forged-room",
                "content": [
                    {"type": "text", "data": "角色资料已找到"},
                    {"type": "markdown", "data": "## 查询结果\n命中 **42** 条记录"},
                    {"type": "image", "data": f"base64://{image_data}"},
                    {"type": "node", "data": [{"type": "text", "data": "附加说明"}]},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        url="https://gscore.example.test/v1/message",
    )
    captured: dict[str, object] = {}

    class FakeClient:
        def post_json(self, url, payload, **kwargs):
            captured.update({"url": url, "payload": payload, "kwargs": kwargs})
            return response

    monkeypatch.setattr(module, "PluginHttpClient", lambda: FakeClient())
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, _request(identity))

    assert result.ok is True and result.status == "completed"
    assert result.output["provider_status"] == "completed"
    assert result.output["reply_count"] == 4
    assert result.output["replies"] == [
        {"type": "text", "content": "角色资料已找到"},
        {"type": "text", "content": "查询结果\n命中 42 条记录"},
        {"type": "image", "base64": image_data},
        {"type": "text", "content": "附加说明"},
    ]
    assert set(result.output) == {"provider_status", "reply_count", "replies"}
    assert captured["url"] == "https://gscore.example.test/v1/message"
    request_body = captured["payload"]
    assert request_body["content"] == [{"type": "text", "data": "查询角色"}]
    assert request_body["group_id"] == "group-real-100"
    assert request_body["user_id"] == "member-real-200"
    assert request_body["sender"]["nickname"] == "真实成员"
    assert request_body["msg_id"] == "message-real-300"
    assert request_body["bot_id"] == f"mbs-bot-{identity['bot_id']}"
    assert "999" not in json.dumps(request_body, ensure_ascii=False)
    assert captured["kwargs"]["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer tenant-gscore-key",
    }


def test_gscore_adapter_does_not_connect_when_unconfigured(
    gscore_fixture,
    monkeypatch,
) -> None:
    """缺少租户地址、密钥时直接结束，不能建立任何外部连接。"""

    factory, identity = gscore_fixture
    loaded = load_directory_plugin("gscore_adapter", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("未启用或未配置时不应创建 HTTP 客户端")

    monkeypatch.setattr(module, "PluginHttpClient", ForbiddenClient)
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        unconfigured = module.execute_message_action(
            db,
            _request(identity, config={"enabled": True, "api_url": "", "api_key": ""}),
        )

    assert unconfigured.ok is False and unconfigured.output["provider_status"] == "not_configured"


def test_gscore_adapter_rejects_untrusted_context_and_invalid_response(
    gscore_fixture,
    monkeypatch,
) -> None:
    """缺少可信上下文或外部返回非标准消息时，不生成可投递结果。"""

    factory, identity = gscore_fixture
    loaded = load_directory_plugin("gscore_adapter", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class InvalidResponseClient:
        def post_json(self, url, payload, **kwargs):
            del payload, kwargs
            return PluginHttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=b'{"target_id":"other","content":[{"type":"video","data":"bad"}]}',
                url=url,
            )

    monkeypatch.setattr(module, "PluginHttpClient", lambda: InvalidResponseClient())
    forged = _request(identity)
    forged = PluginExecutionRequest(
        action=forged.action,
        config=forged.config,
        payload=forged.payload,
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=identity["message_id"],
            sender_id="forged-user",
            channel_type="telegram",
            chat_type="group",
            message_type="text",
        ),
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        untrusted = module.execute_message_action(db, forged)
        invalid = module.execute_message_action(db, _request(identity))

    assert untrusted.ok is False and untrusted.output["provider_status"] == "trusted_context_required"
    assert invalid.ok is False and invalid.output["provider_status"] == "response_invalid"
