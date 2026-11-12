"""咕嘎助手 Agent 接入地址与按渠道隔离的设备凭据。"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.models.entities import Channel
from app.services.security import decrypt_secret, encrypt_secret

from .contract import ACCESS_KEY, ONLINE_TTL_SECONDS, RUNTIME_KEY, as_object, text
from .service import _runtime, _save_runtime


def initialize_connection(channel: Channel, public_base_url: str) -> None:
    """渠道有主键后生成 Agent 地址与随机凭据;重复调用不会更换 Token。"""

    base = text(public_base_url).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("咕嘎助手服务地址必须是有效的 HTTP 或 HTTPS 地址")
    channel.api_base = f"{base}/api/guga-agent/{int(channel.id)}"
    runtime = _runtime(channel)
    access = as_object(runtime.get(ACCESS_KEY))
    if not text(access.get("token_encrypted")):
        access["token_encrypted"] = encrypt_secret(secrets.token_urlsafe(32))
        runtime[ACCESS_KEY] = access
        _save_runtime(channel, runtime)


def _connection_token(channel: Channel) -> str:
    access = as_object(_runtime(channel).get(ACCESS_KEY))
    return decrypt_secret(access.get("token_encrypted")) or ""


def connection_url(channel: Channel) -> str:
    token = _connection_token(channel)
    base = text(channel.api_base).rstrip("/")
    return f"{base}/{token}" if base and token else ""


def verify_connection_token(channel: Channel, token: str) -> bool:
    expected = _connection_token(channel)
    return bool(expected and token and hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8")))


def account_info(channel: Channel) -> dict[str, object]:
    """确认页只返回展示所需信息,不返回 Token 或设备会话。"""

    runtime = _runtime(channel)
    registration = as_object(runtime.get(RUNTIME_KEY))
    profile = as_object(runtime.get("account_profile"))
    account_id = text(registration.get("account_id"))
    avatar = text(profile.get("avatar_url"))
    if urlsplit(avatar).scheme not in {"http", "https"}:
        avatar = ""
    return {
        "account_id": account_id,
        # 注册阶段手机只上报 wxid；昵称等群名册回填后再显示，不能把 wxid 当昵称。
        "nickname": text(profile.get("name")),
        "avatar_url": avatar,
        "runtime_version": text(registration.get("runtime_version")),
        "device_id": text(registration.get("device_id")),
        "transport": text(registration.get("transport")) or "http",
        "online": _registration_online(registration),
        "last_seen_at": text(registration.get("last_seen_at")),
    }


def _registration_online(registration: dict[str, object]) -> bool:
    if not registration:
        return False
    raw = text(registration.get("last_seen_at"))
    if not raw:
        return bool(registration.get("online"))
    try:
        seen = datetime.fromisoformat(raw)
    except ValueError:
        return bool(registration.get("online"))
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - seen).total_seconds() <= ONLINE_TTL_SECONDS
