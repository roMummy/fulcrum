"""插件绑定页代理字段的保存、校验和展示测试。"""

from __future__ import annotations

import base64
import json

import pytest

from app.core.settings import settings
from app.models.entities import BotPlugin, Plugin
from app.services.plugin_runtime.proxy import (
    PluginProxyConfigError,
    normalize_bypass,
    parse_proxy_config,
    parse_proxy_url,
    strip_proxy_config,
)
from app.services.plugin_schemas import merge_schema_config
from app.services.plugin_secrets import encrypt_plugin_secret_config
from app.web.plugins.views import build_binding_schema_cards


@pytest.fixture(autouse=True)
def _plugin_secret_key(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "plugin_secret_key",
        base64.urlsafe_b64encode(b"r" * 32).decode("ascii"),
    )


def test_proxy_config_validation_and_bypass() -> None:
    endpoint = parse_proxy_config(
        "socks5h://proxy.example.test:1080",
        username="user",
        password="pass",
        bypass="localhost, *.internal.test",
    )
    assert endpoint is not None
    assert (endpoint.proxy_type, endpoint.host, endpoint.port) == ("socks5h", "proxy.example.test", 1080)
    assert (endpoint.username, endpoint.password) == ("user", "pass")
    assert endpoint.bypasses("api.internal.test") is True
    assert endpoint.bypasses("127.0.0.1") is True
    assert parse_proxy_config("") is None

    with pytest.raises(PluginProxyConfigError, match="scheme_required"):
        parse_proxy_config("proxy.example.test:1080")
    with pytest.raises(PluginProxyConfigError, match="scheme_invalid"):
        parse_proxy_config("https://proxy.example.test:1080")
    with pytest.raises(PluginProxyConfigError, match="port_invalid"):
        parse_proxy_config("http://proxy.example.test")
    with pytest.raises(PluginProxyConfigError, match="credentials_inline"):
        parse_proxy_config("http://user:pass@proxy.example.test:1080")
    with pytest.raises(PluginProxyConfigError, match="credentials_incomplete"):
        parse_proxy_config("http://proxy.example.test:1080", username="user")
    with pytest.raises(PluginProxyConfigError, match="invalid"):
        parse_proxy_config("http://proxy.example.test:1080/path")

    legacy = parse_proxy_url("socks5://user:pass@proxy.example.test:1080")
    assert legacy is not None and (legacy.username, legacy.password) == ("user", "pass")


def test_schema_less_plugin_saves_proxy_fields() -> None:
    result = merge_schema_config(
        "daily_news",
        {
            "proxy_url": "http://proxy.example.test:8080",
            "proxy_username": "user",
            "proxy_password": "pass",
            "proxy_bypass": "localhost, .internal.test",
        },
        existing_config_json=json.dumps({"push_time": "08:30"}),
    )
    assert result.secret_patch == {"proxy_password": "pass"}
    config = json.loads(result.config_json or "{}")
    assert config["proxy_url"] == "http://proxy.example.test:8080"
    assert config["proxy_username"] == "user"
    assert config["proxy_bypass"] == "localhost,internal.test"
    assert config["push_time"] == "08:30"
    assert "proxy_password" not in config


def test_schema_plugin_can_save_proxy_alongside_schema_fields() -> None:
    result = merge_schema_config(
        "weather",
        {
            "schema__daily_push_time": "08:00",
            "proxy_url": "socks5://127.0.0.1:1080",
            "proxy_username": "",
            "proxy_password": "",
            "proxy_bypass": "",
        },
        existing_config_json="{}",
    )
    assert result.secret_patch == {}
    config = json.loads(result.config_json or "{}")
    assert config["daily_push_time"] == "08:00"
    assert config["proxy_url"] == "socks5://127.0.0.1:1080"
    assert "proxy_username" not in config and "proxy_bypass" not in config


def test_blank_password_keeps_existing_password() -> None:
    result = merge_schema_config(
        "daily_news",
        {
            "proxy_url": "http://proxy.example.test:8080",
            "proxy_username": "user",
            "proxy_password": "",
            "proxy_bypass": "",
        },
        existing_config_json="{}",
        existing_proxy_password="old-pass",
    )
    assert result.secret_patch == {"proxy_password": "old-pass"}


def test_proxy_can_be_explicitly_cleared() -> None:
    result = merge_schema_config(
        "daily_news",
        {
            "proxy_url": "",
            "proxy_username": "",
            "proxy_password": "",
            "proxy_bypass": "",
            "clear_proxy_url": "true",
        },
        existing_config_json=json.dumps({"proxy_bypass": "old.test"}),
    )
    assert result.secret_patch == {}
    assert set(result.clear_secret_fields) == {"proxy_url", "proxy_password"}
    config = json.loads(result.config_json or "{}")
    assert "proxy_bypass" not in config


def test_invalid_proxy_url_is_rejected_on_save() -> None:
    with pytest.raises(ValueError, match="代理地址需要"):
        merge_schema_config(
            "daily_news",
            {"proxy_url": "proxy.example.test:8080", "proxy_username": "", "proxy_password": ""},
        )

    with pytest.raises(ValueError, match="用户名和密码"):
        merge_schema_config(
            "daily_news",
            {"proxy_url": "http://proxy.example.test:8080", "proxy_username": "user", "proxy_password": ""},
        )


def test_binding_cards_expose_proxy_state_without_leaking_secret(monkeypatch) -> None:
    binding = BotPlugin(
        id=9,
        tenant_id=1,
        bot_id=2,
        plugin_id=3,
        status="enabled",
        config_json=json.dumps({
            "proxy_url": "http://proxy.example.test:8080",
            "proxy_username": "user",
            "proxy_bypass": ".internal.test",
            "push_time": "08:30",
        }),
    )
    binding.plugin = Plugin(id=3, code="daily_news", display_name="每日新闻", plugin_type="scheduled")
    binding.secret_config_encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=9,
        config={"proxy_password": "secret"},
    )
    binding.secret_config_version = "v1"
    monkeypatch.setattr("app.web.plugins.views.build_binding_runtime_view", lambda _binding: {})

    (card,) = build_binding_schema_cards([binding])
    assert card["proxy_view"] == {
        "mode": "proxy",
        "has_proxy": True,
        "url": "http://proxy.example.test:8080",
        "username": "user",
        "has_password": True,
        "bypass": ".internal.test",
    }
    raw_config = str(card["config_view"]["raw_config_json"])
    assert all(key not in raw_config for key in ("proxy_url", "proxy_username", "proxy_bypass"))
    assert "secret" not in raw_config


def test_legacy_single_url_secret_is_displayed_without_credentials(monkeypatch) -> None:
    binding = BotPlugin(
        id=10,
        tenant_id=1,
        bot_id=2,
        plugin_id=3,
        status="enabled",
        config_json="{}",
    )
    binding.plugin = Plugin(id=3, code="daily_news", display_name="每日新闻", plugin_type="scheduled")
    binding.secret_config_encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=10,
        config={"proxy_url": "http://user:secret@proxy.example.test:8080"},
    )
    binding.secret_config_version = "v1"
    monkeypatch.setattr("app.web.plugins.views.build_binding_runtime_view", lambda _binding: {})

    (card,) = build_binding_schema_cards([binding])
    assert card["proxy_view"] == {
        "mode": "proxy",
        "has_proxy": True,
        "url": "http://proxy.example.test:8080",
        "username": "user",
        "has_password": True,
        "bypass": "",
    }


def test_proxy_mode_direct_clears_saved_proxy() -> None:
    result = merge_schema_config(
        "daily_news",
        {"proxy_mode": "direct"},
        existing_config_json=json.dumps({
            "proxy_url": "http://proxy.example.test:8080",
            "proxy_username": "user",
            "proxy_bypass": "old.test",
        }),
        existing_proxy_password="old-pass",
    )
    assert result.secret_patch == {}
    assert set(result.clear_secret_fields) == {"proxy_url", "proxy_password"}
    config = json.loads(result.config_json or "{}")
    assert all(key not in config for key in ("proxy_url", "proxy_username", "proxy_bypass"))


def test_proxy_mode_proxy_requires_address() -> None:
    with pytest.raises(ValueError, match="代理地址"):
        merge_schema_config("daily_news", {"proxy_mode": "proxy", "proxy_url": ""})


def test_strip_proxy_config_removes_binding_proxy_keys() -> None:
    assert strip_proxy_config({
        "proxy_url": "x",
        "proxy_username": "u",
        "proxy_password": "p",
        "proxy_bypass": "y",
        "push_time": "08:30",
    }) == {"push_time": "08:30"}
    assert normalize_bypass("localhost, *.example.com") == ("localhost", "example.com")
