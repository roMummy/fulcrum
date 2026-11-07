from __future__ import annotations

import base64

import pytest

from app.core.settings import Settings, settings
from app.services.plugin_secrets import (
    PLUGIN_SECRET_VERSION,
    PluginSecretError,
    decrypt_plugin_secret_config,
    encrypt_plugin_secret_config,
    plugin_secret_configured,
)


def _install_test_key(monkeypatch: pytest.MonkeyPatch, seed: bytes = b"k") -> None:
    """为单个测试安装确定的 32 字节主密钥，不读取开发或部署环境配置。"""

    raw_key = (seed * 32)[:32]
    encoded = base64.urlsafe_b64encode(raw_key).decode("ascii")
    monkeypatch.setattr(settings, "plugin_secret_key", encoded)


def test_plugin_secret_key_reads_the_confirmed_environment_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """正式配置只读取已确认的 ABOT_PLUGIN_SECRET_KEY，不依赖字段名隐式映射。"""

    encoded = base64.urlsafe_b64encode(b"e" * 32).decode("ascii")
    monkeypatch.setenv("ABOT_PLUGIN_SECRET_KEY", encoded)
    monkeypatch.delenv("PLUGIN_SECRET_KEY", raising=False)

    assert Settings(_env_file=None).plugin_secret_key == encoded

    # 已废弃的隐式字段名不能继续作为隐藏兼容入口，否则部署环境可能误用两套主密钥。
    monkeypatch.delenv("ABOT_PLUGIN_SECRET_KEY")
    monkeypatch.setenv("PLUGIN_SECRET_KEY", encoded)
    assert Settings(_env_file=None).plugin_secret_key == ""


def test_plugin_secret_round_trip_does_not_expose_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    """密文可以在原租户绑定解密，但不能包含可搜索的密钥原文。"""

    _install_test_key(monkeypatch)
    encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=10,
        config={"api_key": "tenant-secret-value", "token": "token-value"},
    )

    assert encrypted is not None
    assert encrypted.startswith(f"{PLUGIN_SECRET_VERSION}.")
    assert "tenant-secret-value" not in encrypted
    assert plugin_secret_configured(encrypted) is True
    assert decrypt_plugin_secret_config(tenant_id=1, binding_id=10, encrypted=encrypted) == {
        "api_key": "tenant-secret-value",
        "token": "token-value",
    }


@pytest.mark.parametrize(
    ("tenant_id", "binding_id"),
    [(2, 10), (1, 11)],
)
def test_plugin_secret_rejects_cross_scope_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: int,
    binding_id: int,
) -> None:
    """复制密文到其他租户或其他插件绑定后必须认证失败。"""

    _install_test_key(monkeypatch)
    encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=10,
        config={"api_key": "tenant-secret-value"},
    )

    with pytest.raises(PluginSecretError, match="plugin_secret_decrypt_failed"):
        decrypt_plugin_secret_config(
            tenant_id=tenant_id,
            binding_id=binding_id,
            encrypted=encrypted,
        )


def test_plugin_secret_rejects_wrong_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """主密钥不一致时返回稳定错误，不能把乱码当作历史明文。"""

    _install_test_key(monkeypatch, b"a")
    encrypted = encrypt_plugin_secret_config(
        tenant_id=1,
        binding_id=10,
        config={"api_key": "tenant-secret-value"},
    )
    _install_test_key(monkeypatch, b"b")

    with pytest.raises(PluginSecretError, match="plugin_secret_decrypt_failed"):
        decrypt_plugin_secret_config(tenant_id=1, binding_id=10, encrypted=encrypted)


def test_plugin_secret_requires_explicit_valid_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少或长度错误的主密钥时直接拒绝保存，不使用弱默认值。"""

    monkeypatch.setattr(settings, "plugin_secret_key", "")
    with pytest.raises(PluginSecretError, match="plugin_secret_key_missing"):
        encrypt_plugin_secret_config(tenant_id=1, binding_id=10, config={"api_key": "secret"})

    monkeypatch.setattr(settings, "plugin_secret_key", base64.urlsafe_b64encode(b"short").decode("ascii"))
    with pytest.raises(PluginSecretError, match="plugin_secret_key_invalid"):
        encrypt_plugin_secret_config(tenant_id=1, binding_id=10, config={"api_key": "secret"})


def test_plugin_secret_empty_config_does_not_require_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有敏感字段时无需创建空密文，普通插件不被主密钥配置阻塞。"""

    monkeypatch.setattr(settings, "plugin_secret_key", "")

    assert encrypt_plugin_secret_config(tenant_id=1, binding_id=10, config={}) is None
    assert decrypt_plugin_secret_config(tenant_id=1, binding_id=10, encrypted=None) == {}
    assert plugin_secret_configured(None) is False
