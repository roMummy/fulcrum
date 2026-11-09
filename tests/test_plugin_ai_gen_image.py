from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-ai-gen-image"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"ai-image-result"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Message, MessageAttachment, Plugin
from app.models.saas import Tenant
from app.services.plugin_directory import get_discovered_plugin, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse


@pytest.fixture()
def image_plugin_fixture(tmp_path: Path):
    """建立真实租户、机器人、渠道、会话和绘图命令消息。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'ai-image.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="ai-image", name="图片租户", status="active")
        db.add(tenant)
        db.flush()
        plugin = Plugin(
            code="ai_gen_image",
            display_name="AI 绘图",
            plugin_type="command",
        )
        db.add(plugin)
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="创意机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="创意微信",
            channel_type="wechat_ipad",
            status="online",
            is_enabled=True,
        )
        db.add(channel)
        db.flush()
        conversation = Conversation(
            tenant_id=tenant.id,
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type="wechat_ipad",
            external_id="image-room@chatroom",
            title="创意讨论群",
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
            sender_id="designer-100",
            sender_name="设计师",
            content="/画图 雪山下的未来城市",
            message_type="text",
            external_message_id="image-message-100",
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
            "enabled": True,
            "api_base": "https://images.example.test/v1",
            "api_key": "tenant-image-key",
            "model": "gpt-image-1",
            "request_timeout_seconds": 90,
        },
        payload={
            "content": "画图 伪造提示词",
            "prompt": "伪造提示词",
            "model": "forged-model",
            "api_base": "http://127.0.0.1/private",
            "attachments": [
                {
                    "type": "image",
                    "storage_path": "/etc/passwd",
                    "status": "downloaded",
                }
            ],
        },
        context=PluginExecutionContext(
            tenant_id=identity["tenant_id"],
            binding_id=identity["binding_id"],
            bot_id=identity["bot_id"],
            channel_id=identity["channel_id"],
            conversation_id=identity["conversation_id"],
            message_id=identity["message_id"],
            sender_id="designer-100",
            channel_type="wechat_ipad",
            chat_type="group",
            message_type="text",
        ),
    )


def _image_response(url: str) -> PluginHttpResponse:
    return PluginHttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "created": 1,
                "provider_trace": "must-not-leak",
                "data": [
                    {
                        "b64_json": base64.b64encode(PNG_BYTES).decode("ascii"),
                        "revised_prompt": "provider-private-prompt",
                    }
                ],
            }
        ).encode("utf-8"),
        url=url,
    )


def test_ai_gen_image_declares_current_commands_and_tenant_model_config() -> None:
    """目录声明四个业务命令，模型地址归租户普通配置，密钥进入绑定密文。"""

    discovered = get_discovered_plugin("ai_gen_image", force=True)
    assert discovered is not None and discovered.is_valid
    assert discovered.manifest.plugin_type == "command"
    assert discovered.matcher.commands == ("AI绘图", "绘图", "画图", "生成图片")
    assert discovered.secret_fields == ("api_key",)
    assert discovered.runtime_config["api_base"] == ""
    assert discovered.runtime_config["model"] == ""
    assert "api_key" not in discovered.runtime_config
    assert "telegram" in discovered.capability.channel_types

    source = (PROJECT_ROOT / "plugins/ai_gen_image/plugin.py").read_text(encoding="utf-8")
    assert "requests" not in source
    assert "WechatAPIClient" not in source
    assert "LLMRegistry" not in source
    assert "GroupBotManager" not in source
    assert "legacy" not in source.lower()


def test_ai_gen_image_uses_trusted_prompt_and_returns_in_memory_image(
    image_plugin_fixture,
    monkeypatch,
) -> None:
    """文本生图只使用落库命令和租户配置，供应商附加字段不会进入结果。"""

    factory, identity = image_plugin_fixture
    loaded = load_directory_plugin("ai_gen_image", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    captured: dict[str, object] = {}

    class FakeClient:
        def post_json(self, url, payload, **kwargs):
            captured.update({"url": url, "payload": payload, "kwargs": kwargs})
            return _image_response(url)

        def request(self, *args, **kwargs):
            raise AssertionError("没有可信图片附件时不应调用编辑接口")

    monkeypatch.setattr(module, "PluginHttpClient", lambda: FakeClient())
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, _request(identity))

    expected_base64 = base64.b64encode(PNG_BYTES).decode("ascii")
    assert result.ok is True and result.status == "completed"
    assert result.output == {
        "provider_status": "completed",
        "mode": "generation",
        "replies": [{"type": "image", "base64": expected_base64}],
    }
    assert captured["url"] == "https://images.example.test/v1/images/generations"
    assert captured["payload"] == {
        "model": "gpt-image-1",
        "prompt": "雪山下的未来城市",
        "response_format": "b64_json",
    }
    assert captured["kwargs"]["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer tenant-image-key",
    }
    serialized = json.dumps(result.output, ensure_ascii=False)
    assert "provider-private-prompt" not in serialized
    assert "must-not-leak" not in serialized
    assert "forged-model" not in json.dumps(captured, ensure_ascii=False, default=str)
    assert "127.0.0.1" not in json.dumps(captured, ensure_ascii=False, default=str)


def test_ai_gen_image_uses_owned_downloaded_attachment_for_edit(
    image_plugin_fixture,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """图片编辑只读取当前消息已下载的可信附件，并构造受限 multipart 请求。"""

    factory, identity = image_plugin_fixture
    loaded = load_directory_plugin("ai_gen_image", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module
    owned_root = tmp_path / "tenant-media"
    owned_root.mkdir(parents=True)
    reference_image = owned_root / "reference.png"
    reference_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"trusted-reference")
    monkeypatch.setattr(module.settings, "data_dir", str(owned_root))
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        db.add(
            MessageAttachment(
                tenant_id=identity["tenant_id"],
                message_id=identity["message_id"],
                raw_event_id=None,
                channel_id=identity["channel_id"],
                conversation_id=identity["conversation_id"],
                direction="in",
                attachment_type="image",
                provider="wechat_ipad",
                status="downloaded",
                storage_path=str(reference_image),
                mime_type="image/png",
                file_size=reference_image.stat().st_size,
            )
        )
        db.commit()

    captured: dict[str, object] = {}

    class FakeClient:
        def post_json(self, *args, **kwargs):
            raise AssertionError("存在可信图片附件时不应调用普通生成接口")

        def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, "kwargs": kwargs})
            return _image_response(url)

    monkeypatch.setattr(module, "PluginHttpClient", lambda: FakeClient())
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, _request(identity))

    assert result.ok is True and result.output["mode"] == "edit"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://images.example.test/v1/images/edits"
    headers = captured["kwargs"]["headers"]
    assert headers["Authorization"] == "Bearer tenant-image-key"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    body = captured["kwargs"]["body"]
    assert b'name="prompt"' in body and "雪山下的未来城市".encode("utf-8") in body
    assert b'name="model"' in body and b"gpt-image-1" in body
    assert b'name="response_format"' in body and b"b64_json" in body
    assert b'name="image"; filename="reference.png"' in body
    assert b"trusted-reference" in body
    assert b"/etc/passwd" not in body


def test_ai_gen_image_stops_before_http_when_disabled_unconfigured_or_untrusted(
    image_plugin_fixture,
    monkeypatch,
) -> None:
    """缺少租户模型配置或可信身份不一致时都不能访问图片服务。"""

    factory, identity = image_plugin_fixture
    loaded = load_directory_plugin("ai_gen_image", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class ForbiddenClient:
        def __init__(self):
            raise AssertionError("前置校验失败时不应创建 HTTP 客户端")

    monkeypatch.setattr(module, "PluginHttpClient", ForbiddenClient)
    request = _request(identity)
    forged_context = PluginExecutionContext(
        tenant_id=identity["tenant_id"],
        binding_id=identity["binding_id"],
        bot_id=identity["bot_id"],
        channel_id=identity["channel_id"],
        conversation_id=identity["conversation_id"],
        message_id=identity["message_id"],
        sender_id="forged-user",
        channel_type="wechat_ipad",
        chat_type="group",
        message_type="text",
    )
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        unconfigured = module.execute_message_action(
            db,
            _request(identity, config={"enabled": True, "api_base": "", "api_key": "", "model": ""}),
        )
        untrusted = module.execute_message_action(
            db,
            PluginExecutionRequest(
                action=request.action,
                config=request.config,
                payload=request.payload,
                context=forged_context,
            ),
        )

    assert unconfigured.output["provider_status"] == "not_configured"
    assert untrusted.output["provider_status"] == "trusted_context_required"


def test_ai_gen_image_rejects_non_image_provider_payload(image_plugin_fixture, monkeypatch) -> None:
    """供应商返回的 base64 不是受支持图片时，不能进入渠道图片队列。"""

    factory, identity = image_plugin_fixture
    loaded = load_directory_plugin("ai_gen_image", force=True)
    assert loaded is not None and loaded.instance is not None
    module = loaded.instance._module

    class InvalidClient:
        def post_json(self, url, payload, **kwargs):
            del payload, kwargs
            return PluginHttpResponse(
                status_code=200,
                headers={"content-type": "application/json"},
                body=json.dumps({"data": [{"b64_json": base64.b64encode(b"not-an-image").decode("ascii")}]}).encode(),
                url=url,
            )

    monkeypatch.setattr(module, "PluginHttpClient", lambda: InvalidClient())
    with tenant_scope(identity["tenant_id"]), tenant_session(factory) as db:
        result = module.execute_message_action(db, _request(identity))

    assert result.ok is False
    assert result.output["provider_status"] == "response_invalid"
    assert result.output["replies"][0]["type"] == "text"
