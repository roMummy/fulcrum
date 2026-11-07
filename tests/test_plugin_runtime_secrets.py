from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-runtime-secrets"

# 运行时服务会初始化日志和数据库设置，所有路径必须在导入 app 前锁定到测试目录。
os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Bot, BotPlugin, Plugin
from app.models.saas import Tenant
from app.services.plugin_directory import (
    PluginDispatchCandidate,
    PluginDispatchContext,
    _build_directory_context,
    discover_plugins,
)
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionResult
from app.services.plugin_runtime.executors import DirectoryRuntimeExecutor
from app.services.plugin_runtime.service import execute_binding_action, execute_binding_message_action
from app.services.plugin_secret_cleanup import scrub_tenant_plugin_plaintext_secrets
from app.services.plugin_secrets import encrypt_plugin_secret_config


@pytest.fixture(autouse=True)
def plugin_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行时密钥测试使用固定测试主密钥，确保密文结果与部署配置无关。"""

    monkeypatch.setattr(
        settings,
        "plugin_secret_key",
        base64.urlsafe_b64encode(b"r" * 32).decode("ascii"),
    )


def test_declared_plugin_secret_defaults_are_physically_empty() -> None:
    """目录清单声明的每个敏感字段都必须是空默认值，不能只依赖扫描阶段过滤。"""

    for discovered in discover_plugins(force=True):
        if not discovered.secret_fields:
            continue
        source = discovered.config_path.read_text(encoding="utf-8")
        for field_name in discovered.secret_fields:
            match = re.search(
                rf"^\s*{re.escape(field_name)}\s*=\s*(['\"])(.*?)\1\s*$",
                source,
                flags=re.MULTILINE,
            )
            assert match is not None, f"{discovered.manifest.code}.{field_name} 缺少空字段声明"
            assert match.group(2) == "", f"{discovered.manifest.code}.{field_name} 仍包含非空默认值"
            assert field_name not in discovered.runtime_config


def test_binding_runtime_merges_ciphertext_and_builds_trusted_context() -> None:
    """后台执行只信任绑定主键，普通 JSON 和 payload 不能伪造密钥或租户身份。"""

    plugin = Plugin(id=7, code="dify", display_name="Dify 对话", plugin_type="ai")
    binding = BotPlugin(
        id=10,
        tenant_id=1,
        bot_id=20,
        plugin_id=7,
        status="enabled",
        config_json=json.dumps({"api_base": "https://tenant.example/v1", "api_key": "legacy-plain"}),
    )
    binding.plugin = plugin
    binding.secret_config_encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=10,
        config={"api_key": "tenant-secret"},
    )
    binding.secret_config_version = "v1"

    captured = {}

    class CaptureExecutor:
        def execute(self, request):
            captured["request"] = request
            return PluginExecutionResult(ok=True, status="completed", message="ok", output={})

    with patch(
        "app.services.plugin_runtime.service.get_plugin_runtime_definition",
        return_value=SimpleNamespace(executor=CaptureExecutor()),
    ):
        result = execute_binding_action(
            binding,
            "execute",
            payload={
                "tenant_id": 999,
                "binding_id": 999,
                "bot_id": 999,
                "channel_id": 30,
                "conversation_id": 40,
                "sender_id": "member-001",
            },
            context=PluginExecutionContext(
                tenant_id=999,
                binding_id=999,
                bot_id=999,
                channel_id=30,
                conversation_id=40,
                sender_id="member-001",
            ),
        )

    request = captured["request"]
    assert result is not None and result.ok is True
    assert request.config["api_key"] == "tenant-secret"
    assert request.config["api_base"] == "https://tenant.example/v1"
    assert "tenant-secret" not in json.dumps(request.payload, ensure_ascii=False)
    assert request.context.tenant_id == 1
    assert request.context.binding_id == 10
    assert request.context.bot_id == 20
    assert request.context.channel_id == 30


def test_payload_identity_fields_are_not_promoted_to_trusted_context() -> None:
    """手工执行载荷即使携带定位字段，也只能作为普通插件输入。"""

    plugin = Plugin(id=7, code="dify", display_name="Dify 对话", plugin_type="ai")
    binding = BotPlugin(id=10, tenant_id=1, bot_id=20, plugin_id=7, status="enabled")
    binding.plugin = plugin
    captured = {}

    class CaptureExecutor:
        def execute(self, request):
            captured["request"] = request
            return PluginExecutionResult(ok=True, status="completed", message="ok", output={})

    with patch(
        "app.services.plugin_runtime.service.get_plugin_runtime_definition",
        return_value=SimpleNamespace(executor=CaptureExecutor()),
    ):
        execute_binding_action(
            binding,
            "execute",
            payload={
                "channel_id": 999,
                "conversation_id": 999,
                "sender_id": "forged-member",
            },
        )

    request = captured["request"]
    assert request.context.tenant_id == 1
    assert request.context.binding_id == 10
    assert request.context.bot_id == 20
    assert request.context.channel_id is None
    assert request.context.conversation_id is None
    assert request.context.sender_id is None


def test_binding_message_runtime_passes_db_without_weakening_identity_or_secret_boundary() -> None:
    """消息入口只增加数据库会话，绑定身份和密钥仍由平台重新构造。"""

    plugin = Plugin(id=7, code="message_summary", display_name="群聊总结", plugin_type="command")
    binding = BotPlugin(
        id=10,
        tenant_id=1,
        bot_id=20,
        plugin_id=7,
        status="enabled",
        config_json=json.dumps({"api_base": "https://tenant.example/v1", "api_key": "legacy-plain"}),
    )
    binding.plugin = plugin
    binding.secret_config_encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=10,
        config={"api_key": "tenant-secret"},
    )
    captured = {}
    expected_db = object()

    class CaptureExecutor:
        def execute_message(self, db, request):
            captured["db"] = db
            captured["request"] = request
            return PluginExecutionResult(ok=True, status="completed", message="ok", output={})

    with patch(
        "app.services.plugin_runtime.service.get_plugin_runtime_definition",
        return_value=SimpleNamespace(executor=CaptureExecutor()),
    ):
        result = execute_binding_message_action(
            expected_db,  # type: ignore[arg-type]
            binding,
            "execute",
            payload={"tenant_id": 999, "conversation_id": 999},
            context=PluginExecutionContext(
                tenant_id=999,
                binding_id=999,
                bot_id=999,
                channel_id=30,
                conversation_id=40,
                chat_type="group",
            ),
        )

    request = captured["request"]
    assert result is not None and result.ok is True
    assert captured["db"] is expected_db
    assert request.config["api_key"] == "tenant-secret"
    assert request.context.tenant_id == 1
    assert request.context.binding_id == 10
    assert request.context.bot_id == 20
    assert request.context.conversation_id == 40
    assert "tenant-secret" not in json.dumps(request.payload, ensure_ascii=False)


def test_directory_runtime_only_passes_db_to_explicit_message_handler() -> None:
    """目录执行器优先调用数据库感知入口，普通插件仍只收到原动作请求。"""

    expected_db = object()
    request = SimpleNamespace(action="execute", config={}, payload={}, context=None)
    captured = {}

    class MessageAwareInstance:
        def execute_message_action(self, db, merged_request):
            captured["message_db"] = db
            captured["message_request"] = merged_request
            return PluginExecutionResult(ok=True, status="completed", message="message", output={})

    with (
        patch(
            "app.services.plugin_runtime.executors.load_directory_plugin",
            return_value=SimpleNamespace(instance=MessageAwareInstance()),
        ),
        patch("app.services.plugin_runtime.executors.get_plugin_default_config", return_value={}),
    ):
        result = DirectoryRuntimeExecutor("message_summary").execute_message(  # type: ignore[arg-type]
            expected_db,  # type: ignore[arg-type]
            request,  # type: ignore[arg-type]
        )

    assert result.ok is True
    assert captured["message_db"] is expected_db

    class OrdinaryInstance:
        def execute_action(self, merged_request):
            captured["ordinary_request"] = merged_request
            return PluginExecutionResult(ok=True, status="completed", message="ordinary", output={})

    with (
        patch(
            "app.services.plugin_runtime.executors.load_directory_plugin",
            return_value=SimpleNamespace(instance=OrdinaryInstance()),
        ),
        patch("app.services.plugin_runtime.executors.get_plugin_default_config", return_value={}),
    ):
        result = DirectoryRuntimeExecutor("weather").execute_message(  # type: ignore[arg-type]
            expected_db,  # type: ignore[arg-type]
            request,  # type: ignore[arg-type]
        )

    assert result.ok is True
    assert result.message == "ordinary"
    assert "ordinary_request" in captured


def test_group_scope_cannot_override_binding_secret() -> None:
    """群级普通配置可以覆盖业务参数，但不能用明文覆盖机器人绑定密文。"""

    plugin = Plugin(id=7, code="dify", display_name="Dify 对话", plugin_type="ai")
    binding = BotPlugin(
        id=10,
        tenant_id=1,
        bot_id=20,
        plugin_id=7,
        status="enabled",
        config_json=json.dumps({"api_base": "https://binding.example/v1", "api_key": "legacy-plain"}),
    )
    binding.plugin = plugin
    binding.secret_config_encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=10,
        config={"api_key": "tenant-secret"},
    )

    static_context = PluginDispatchContext(
        tenant_id=1,
        bot_id=20,
        channel_id=30,
        channel_type="wechat_ipad",
        chat_type="group",
        message_type="text",
        sender_id="member-001",
        conversation_external_id="room-001@chatroom",
        message_text="/ai hello",
    )
    candidate = PluginDispatchCandidate(
        binding_id=10,
        plugin_id=7,
        plugin_code="dify",
        plugin_type="ai",
        priority=100,
        consume_policy="continue",
        timeout_ms=10000,
        entrypoint_name="plugin.py",
        scope_config_override_json=json.dumps(
            {"api_base": "https://group.example/v1", "api_key": "scope-plain"}
        ),
    )

    context = _build_directory_context(
        static_context,
        candidate,
        binding,
        {},
        raw_event_id=50,
        conversation_id=40,
        message_id=60,
    )

    assert context.binding_config["api_base"] == "https://group.example/v1"
    assert context.binding_config["api_key"] == "tenant-secret"


def test_plaintext_cleanup_removes_only_declared_secret_fields(tmp_path: Path) -> None:
    """部署清理删除普通 JSON 明文，但保留非敏感配置和新密文。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'cleanup.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with platform_session(factory) as db:
            tenant = Tenant(code="cleanup", name="清理租户", status="active")
            db.add(tenant)
            db.flush()
            bot = Bot(tenant_id=tenant.id, name="清理机器人", status="running", is_enabled=True)
            plugin = Plugin(code="dify", display_name="Dify 对话", plugin_type="ai")
            db.add_all([bot, plugin])
            db.flush()
            binding = BotPlugin(
                tenant_id=tenant.id,
                bot_id=bot.id,
                plugin_id=plugin.id,
                status="enabled",
                config_json=json.dumps({"api_base": "https://tenant.example/v1", "api_key": "legacy-plain"}),
            )
            db.add(binding)
            db.flush()
            binding.secret_config_encrypted = encrypt_plugin_secret_config(
                tenant_id=tenant.id,
                binding_id=binding.id,
                config={"api_key": "trusted-secret"},
            )
            db.commit()
            identity = (int(tenant.id), int(binding.id), str(binding.secret_config_encrypted))

        tenant_id, binding_id, original_ciphertext = identity
        with tenant_scope(tenant_id), tenant_session(factory) as db:
            summary = scrub_tenant_plugin_plaintext_secrets(db)
            db.commit()
            binding = db.get(BotPlugin, binding_id)
            assert binding is not None
            config = json.loads(binding.config_json or "{}")
            assert config == {"api_base": "https://tenant.example/v1"}
            assert binding.secret_config_encrypted == original_ciphertext
            assert summary == {
                "scanned_bindings": 1,
                "changed_bindings": 1,
                "removed_fields": 1,
            }
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
