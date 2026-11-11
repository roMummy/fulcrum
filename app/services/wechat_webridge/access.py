"""微桥接入地址和按渠道隔离的设备凭据。"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from app.models.entities import Channel
from app.services.security import decrypt_secret, encrypt_secret

from .contract import as_object, text
from .service import _runtime, _save_runtime


def initialize_connection(channel: Channel, public_base_url: str) -> None:
    """渠道有主键后生成随机凭据；数据库地址不保存明文 Token。"""

    base = str(public_base_url or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("微桥服务地址必须是有效的 HTTP 或 HTTPS 地址")
    channel.api_base = f"{base}/api/wechat-webridge/{int(channel.id)}"
    runtime = _runtime(channel)
    runtime["webridge_access"] = {"token_encrypted": encrypt_secret(secrets.token_urlsafe(32))}
    _save_runtime(channel, runtime)


def _connection_token(channel: Channel) -> str:
    access = as_object(_runtime(channel).get("webridge_access"))
    return decrypt_secret(access.get("token_encrypted")) or ""


def connection_url(channel: Channel) -> str:
    token = _connection_token(channel)
    base = str(channel.api_base or "").strip().rstrip("/")
    return f"{base}/{token}" if base and token else ""


def verify_connection_token(channel: Channel, token: str) -> bool:
    expected = _connection_token(channel)
    return bool(expected and token and hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8")))


def account_info(channel: Channel) -> dict[str, object]:
    """确认页只返回账号基础资料，不返回会话、设备密钥或接入 Token。"""

    runtime = _runtime(channel)
    bridge = as_object(runtime.get("webridge"))
    profile = as_object(runtime.get("account_profile"))
    state = as_object(bridge.get("state"))
    avatar = text(profile.get("avatar_url"))
    if urlsplit(avatar).scheme not in {"http", "https"}:
        avatar = ""
    return {
        "account_id": text(bridge.get("account_id")),
        "nickname": text(profile.get("nickname")),
        "avatar_url": avatar,
        "runtime_version": text(bridge.get("runtime_version")),
        "is_logged_in": bool(state.get("is_logged_in")),
    }
