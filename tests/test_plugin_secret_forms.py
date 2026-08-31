from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-secret-forms"

# 本文件会导入带全局日志和数据库设置的应用服务，必须在任何 app 模块导入前
# 固定到测试目录，防止聚焦测试写入开发日志或连接共享数据库。
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
from app.schemas.forms import BotPluginBindCreate
from app.services.crud import bind_plugin, update_binding_config
from app.services.plugin_schemas import build_plugin_config_view, merge_schema_config
from app.services.plugin_secrets import decrypt_plugin_secret_config


@pytest.fixture()
def plugin_database(tmp_path):
    """为插件绑定配置提供独立数据库，避免读取开发环境中的租户和密文。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'plugin-secret-form.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        with platform_session(factory) as db:
            tenant = Tenant(code="secret-form", name="密钥表单租户", status="active")
            db.add(tenant)
            db.flush()
            bot = Bot(tenant_id=tenant.id, name="密钥表单机器人", status="running", is_enabled=True)
            plugin = Plugin(code="dify", display_name="Dify 对话", plugin_type="ai")
            db.add_all([bot, plugin])
            db.commit()
            identity = (int(tenant.id), int(bot.id), int(plugin.id))
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture(autouse=True)
def plugin_secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有保存链路使用测试专属主密钥，不依赖部署环境。"""

    encoded = base64.urlsafe_b64encode(b"f" * 32).decode("ascii")
    monkeypatch.setattr(settings, "plugin_secret_key", encoded)


def test_schema_merge_separates_plain_and_sensitive_values() -> None:
    """API 地址进入普通 JSON，API Key 只能进入密钥变更集合。"""

    result = merge_schema_config(
        "dify",
        {
            "schema__api_base": "https://tenant-dify.example/v1",
            "schema__api_key": "tenant-dify-secret",
            "schema__app_mode": "workflow",
        },
    )

    ordinary = json.loads(result.config_json or "{}")
    assert ordinary["api_base"] == "https://tenant-dify.example/v1"
    assert ordinary["app_mode"] == "workflow"
    assert "api_key" not in ordinary
    assert result.secret_patch == {"api_key": "tenant-dify-secret"}
    assert result.clear_secret_fields == ()


def test_schema_view_only_reports_secret_configuration_state() -> None:
    """页面上下文不包含密钥原文、掩码片段或普通 JSON 中的历史敏感字段。"""

    view = build_plugin_config_view(
        "dify",
        json.dumps({"api_base": "https://tenant-dify.example/v1", "api_key": "legacy-plain-secret"}),
        secret_config={"api_key": "tenant-dify-secret"},
    )

    api_key_field = next(item for item in view["fields"] if item["name"] == "api_key")
    assert api_key_field["render_value"] == ""
    assert api_key_field["secret_configured"] is True
    assert "legacy-plain-secret" not in view["preview_json"]
    assert "tenant-dify-secret" not in str(view)
    assert "api_key" not in json.loads(view["preview_json"])


def test_binding_save_preserves_blank_secret_and_explicit_clear_removes_it(plugin_database) -> None:
    """空密码编辑保留密文，只有明确勾选清除时才删除已经保存的密钥。"""

    factory, (tenant_id, bot_id, plugin_id) = plugin_database
    created_config = merge_schema_config(
        "dify",
        {
            "schema__api_base": "https://tenant-dify.example/v1",
            "schema__api_key": "tenant-dify-secret",
        },
    )

    with tenant_scope(tenant_id), tenant_session(factory) as db:
        binding = bind_plugin(
            db,
            BotPluginBindCreate(
                bot_id=bot_id,
                plugin_id=plugin_id,
                status="enabled",
                config_json=created_config.config_json,
            ),
            secret_config_patch=created_config.secret_patch,
        )
        binding_id = int(binding.id)
        original_ciphertext = str(binding.secret_config_encrypted)
        assert "tenant-dify-secret" not in str(binding.config_json)

    blank_update = merge_schema_config(
        "dify",
        {
            "schema__api_base": "https://tenant-dify.example/v2",
            "schema__api_key": "",
        },
        existing_config_json=json.dumps({"api_base": "https://tenant-dify.example/v1"}),
    )
    with tenant_scope(tenant_id), tenant_session(factory) as db:
        binding = db.get(BotPlugin, binding_id)
        assert binding is not None
        update_binding_config(
            db,
            binding,
            status="enabled",
            config_json=blank_update.config_json,
            secret_config_patch=blank_update.secret_patch,
            clear_secret_fields=blank_update.clear_secret_fields,
        )
        assert binding.secret_config_encrypted == original_ciphertext
        assert decrypt_plugin_secret_config(
            tenant_id=tenant_id,
            binding_id=binding_id,
            encrypted=binding.secret_config_encrypted,
        ) == {"api_key": "tenant-dify-secret"}

    clear_update = merge_schema_config(
        "dify",
        {"schema_clear__api_key": "true"},
        existing_config_json=blank_update.config_json,
    )
    with tenant_scope(tenant_id), tenant_session(factory) as db:
        binding = db.get(BotPlugin, binding_id)
        assert binding is not None
        update_binding_config(
            db,
            binding,
            status="enabled",
            config_json=clear_update.config_json,
            secret_config_patch=clear_update.secret_patch,
            clear_secret_fields=clear_update.clear_secret_fields,
        )
        assert binding.secret_config_encrypted is None
        assert binding.secret_config_version is None
