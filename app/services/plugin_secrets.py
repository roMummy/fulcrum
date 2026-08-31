from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.settings import settings


PLUGIN_SECRET_VERSION = "v1"
PLUGIN_SECRET_NONCE_BYTES = 12


class PluginSecretError(ValueError):
    """插件敏感配置无法安全保存或读取时抛出的统一错误。"""


def encrypt_plugin_secret_config(
    *,
    tenant_id: int,
    binding_id: int,
    config: Mapping[str, Any] | None,
) -> str | None:
    """把插件敏感配置加密为绑定租户和绑定主键的版本化密文。

    AES-GCM 会同时提供机密性和完整性。AAD 不写入业务数据，但会参与认证，
    因而复制数据库密文到其他租户或其他插件绑定后无法通过解密校验。
    """

    normalized = _normalize_secret_config(config)
    if not normalized:
        return None

    key = _load_master_key()
    nonce = os.urandom(PLUGIN_SECRET_NONCE_BYTES)
    plaintext = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(
        nonce,
        plaintext,
        _build_aad(tenant_id=tenant_id, binding_id=binding_id),
    )
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
    return f"{PLUGIN_SECRET_VERSION}.{payload}"


def decrypt_plugin_secret_config(
    *,
    tenant_id: int,
    binding_id: int,
    encrypted: str | None,
) -> dict[str, Any]:
    """解密当前版本插件密文，不接受明文、旧算法或未知版本降级。"""

    normalized = str(encrypted or "").strip()
    if not normalized:
        return {}

    version, separator, payload = normalized.partition(".")
    if separator != "." or version != PLUGIN_SECRET_VERSION or not payload:
        raise PluginSecretError("plugin_secret_version_invalid")

    try:
        decoded = base64.urlsafe_b64decode(_restore_base64_padding(payload))
        if len(decoded) <= PLUGIN_SECRET_NONCE_BYTES:
            raise PluginSecretError("plugin_secret_payload_invalid")
        nonce = decoded[:PLUGIN_SECRET_NONCE_BYTES]
        ciphertext = decoded[PLUGIN_SECRET_NONCE_BYTES:]
        plaintext = AESGCM(_load_master_key()).decrypt(
            nonce,
            ciphertext,
            _build_aad(tenant_id=tenant_id, binding_id=binding_id),
        )
        parsed = json.loads(plaintext.decode("utf-8"))
    except PluginSecretError:
        raise
    except (InvalidTag, ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        # 对外只返回稳定错误代码，不能把密文、主密钥或底层密码库细节写入日志。
        raise PluginSecretError("plugin_secret_decrypt_failed") from exc

    if not isinstance(parsed, dict):
        raise PluginSecretError("plugin_secret_payload_invalid")
    return {str(key): value for key, value in parsed.items()}


def plugin_secret_configured(encrypted: str | None) -> bool:
    """只判断是否存在密文，页面不得通过该函数读取或展示任何密钥片段。"""

    return bool(str(encrypted or "").strip())


def _load_master_key() -> bytes:
    """读取独立插件主密钥，并严格要求 URL-safe Base64 解码后为 32 字节。"""

    encoded = str(settings.plugin_secret_key or "").strip()
    if not encoded:
        raise PluginSecretError("plugin_secret_key_missing")
    try:
        key = base64.urlsafe_b64decode(_restore_base64_padding(encoded))
    except (ValueError, binascii.Error) as exc:
        raise PluginSecretError("plugin_secret_key_invalid") from exc
    if len(key) != 32:
        raise PluginSecretError("plugin_secret_key_invalid")
    return key


def _normalize_secret_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """删除空敏感值，避免把空字符串误判为已经完成凭证配置。"""

    if not config:
        return {}
    return {
        str(key): value
        for key, value in config.items()
        if str(key).strip() and value not in (None, "", [], {})
    }


def _build_aad(*, tenant_id: int, binding_id: int) -> bytes:
    """构造稳定认证上下文，拒绝零值主键绕过租户和绑定边界。"""

    normalized_tenant_id = int(tenant_id)
    normalized_binding_id = int(binding_id)
    if normalized_tenant_id <= 0 or normalized_binding_id <= 0:
        raise PluginSecretError("plugin_secret_scope_invalid")
    return (
        f"multi-bot-saas:plugin-secret:{PLUGIN_SECRET_VERSION}:"
        f"tenant={normalized_tenant_id}:binding={normalized_binding_id}"
    ).encode("utf-8")


def _restore_base64_padding(value: str) -> bytes:
    """恢复 URL-safe Base64 允许省略的尾部 padding。"""

    normalized = str(value or "").strip()
    padded = normalized + ("=" * (-len(normalized) % 4))
    return padded.encode("ascii")
