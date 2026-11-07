"""Windows 本地代理连接器的注册、激活和一次性扫码会话服务。

本模块只保存连接器身份摘要和在线状态。用户本机 SOCKS5 的主机、用户名和密码
始终留在 Windows 连接器中，服务端不会接收也不会把它们写入登录会话。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import (
    LOCAL_PROXY_CONNECTOR_STATUSES,
    ProxyConnector,
    ProxyConnectorActivationCode,
    ProxyConnectorCredential,
)


ACTIVATION_CODE_TTL = timedelta(minutes=15)
DEVICE_KEY_BYTES = 32
CONNECTOR_OFFLINE_AFTER = timedelta(seconds=45)


class LocalProxyConnectorError(ValueError):
    """连接器注册、激活、认证和租户边界错误。"""


def _hash_token(value: str) -> str:
    """只保存令牌摘要；即使数据库被读取也不能反推出激活码或设备密钥。"""

    normalized = str(value or "").strip()
    if not normalized:
        raise LocalProxyConnectorError("connector_token_required")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_name(value: Any) -> str:
    """连接器名称是页面展示标识，限制长度并拒绝空值。"""

    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 100:
        raise LocalProxyConnectorError("connector_name_required")
    return normalized


def create_proxy_connector(db: Session, *, name: str) -> ProxyConnector:
    """在当前可信租户创建待激活连接器。"""

    tenant_id = require_tenant_id()
    normalized_name = _normalize_name(name)
    duplicate = db.scalar(
        select(ProxyConnector).where(
            ProxyConnector.tenant_id == tenant_id,
            ProxyConnector.name == normalized_name,
        )
    )
    if duplicate is not None:
        raise LocalProxyConnectorError("connector_name_exists")
    connector = ProxyConnector(tenant_id=tenant_id, name=normalized_name, status="pending")
    db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


def issue_activation_code(
    db: Session,
    *,
    connector_id: int,
    created_by_user_id: int | None = None,
    ttl: timedelta = ACTIVATION_CODE_TTL,
) -> str:
    """生成一次性激活码；原文只由当前响应返回，不写入数据库。"""

    tenant_id = require_tenant_id()
    connector = db.scalar(
        select(ProxyConnector).where(
            ProxyConnector.id == int(connector_id),
            ProxyConnector.tenant_id == tenant_id,
        )
    )
    if connector is None:
        raise LocalProxyConnectorError("connector_not_found")
    if connector.status == "disabled":
        raise LocalProxyConnectorError("connector_disabled")
    code = f"{secrets.randbelow(1_000_000_000):09d}-{secrets.token_hex(4).upper()}"
    db.add(
        ProxyConnectorActivationCode(
            tenant_id=tenant_id,
            connector_id=connector.id,
            code_hash=_hash_token(code),
            expires_at=now_shanghai() + ttl,
            created_by_user_id=created_by_user_id,
        )
    )
    db.commit()
    return code


def consume_activation_code(
    db: Session,
    *,
    code: str,
    device_id: str,
    client_version: str = "",
    local_proxy_label: str = "",
) -> dict[str, str | int]:
    """消费一次性激活码并签发设备密钥。"""

    normalized_device_id = str(device_id or "").strip()
    if not normalized_device_id or len(normalized_device_id) > 128:
        raise LocalProxyConnectorError("connector_device_id_invalid")
    activation = db.scalar(
        select(ProxyConnectorActivationCode).where(
            ProxyConnectorActivationCode.code_hash == _hash_token(code)
        )
    )
    now = now_shanghai()
    if activation is None or activation.consumed_at is not None or activation.revoked_at is not None:
        raise LocalProxyConnectorError("connector_activation_code_invalid")
    if ensure_shanghai_datetime(activation.expires_at) <= now:
        raise LocalProxyConnectorError("connector_activation_code_expired")
    connector = db.scalar(
        select(ProxyConnector).where(
            ProxyConnector.id == activation.connector_id,
            ProxyConnector.tenant_id == activation.tenant_id,
        )
    )
    if connector is None or connector.status == "disabled":
        raise LocalProxyConnectorError("connector_disabled")
    existing_device = db.scalar(
        select(ProxyConnector).where(
            ProxyConnector.device_id == normalized_device_id,
            ProxyConnector.id != connector.id,
        )
    )
    if existing_device is not None:
        raise LocalProxyConnectorError("connector_device_already_bound")

    device_key = secrets.token_urlsafe(DEVICE_KEY_BYTES)
    activation.consumed_at = now
    connector.device_id = normalized_device_id
    connector.status = "active"
    connector.client_version = str(client_version or "").strip()[:50] or None
    connector.local_proxy_label = str(local_proxy_label or "").strip()[:255] or None
    connector.last_seen_at = now
    db.add(
        ProxyConnectorCredential(
            tenant_id=connector.tenant_id,
            connector_id=connector.id,
            device_key_hash=_hash_token(device_key),
            issued_at=now,
        )
    )
    db.commit()
    return {"connector_id": int(connector.id), "tenant_id": int(connector.tenant_id), "device_key": device_key}


def authenticate_device(
    db: Session,
    *,
    device_key: str,
    client_version: str = "",
    local_proxy_label: str = "",
) -> ProxyConnector:
    """按设备密钥恢复可信连接器和租户归属，并刷新在线状态。"""

    credential = db.scalar(
        select(ProxyConnectorCredential).where(
            ProxyConnectorCredential.device_key_hash == _hash_token(device_key),
            ProxyConnectorCredential.revoked_at.is_(None),
        )
    )
    if credential is None:
        raise LocalProxyConnectorError("connector_device_key_invalid")
    connector = db.scalar(
        select(ProxyConnector).where(
            ProxyConnector.id == credential.connector_id,
            ProxyConnector.tenant_id == credential.tenant_id,
            ProxyConnector.status == "active",
        )
    )
    if connector is None:
        raise LocalProxyConnectorError("connector_disabled")
    now = now_shanghai()
    connector.last_seen_at = now
    connector.client_version = str(client_version or connector.client_version or "").strip()[:50] or None
    connector.local_proxy_label = str(local_proxy_label or connector.local_proxy_label or "").strip()[:255] or None
    credential.last_used_at = now
    db.commit()
    db.refresh(connector)
    return connector


def mark_connector_heartbeat(db: Session, *, connector: ProxyConnector) -> None:
    """更新连接器心跳；连接器身份必须已经由 authenticate_device 校验。"""

    connector.last_seen_at = now_shanghai()
    db.commit()


def disable_proxy_connector(db: Session, *, connector_id: int) -> None:
    """停用连接器并吊销全部设备密钥，后续 WebSocket 和扫码会话都会拒绝。"""

    tenant_id = require_tenant_id()
    connector = db.scalar(
        select(ProxyConnector).where(
            ProxyConnector.id == int(connector_id),
            ProxyConnector.tenant_id == tenant_id,
        )
    )
    if connector is None:
        raise LocalProxyConnectorError("connector_not_found")
    connector.status = "disabled"
    connector.disabled_at = now_shanghai()
    for credential in db.scalars(
        select(ProxyConnectorCredential).where(ProxyConnectorCredential.connector_id == connector.id)
    ).all():
        credential.revoked_at = now_shanghai()
    db.commit()


def list_proxy_connectors(db: Session) -> list[dict[str, object]]:
    """返回租户页面需要的连接器摘要，不返回激活码和设备密钥。"""

    tenant_id = require_tenant_id()
    now = now_shanghai()
    rows = db.scalars(
        select(ProxyConnector)
        .where(ProxyConnector.tenant_id == tenant_id)
        .order_by(ProxyConnector.id.desc())
    ).all()
    result: list[dict[str, object]] = []
    for connector in rows:
        last_seen = ensure_shanghai_datetime(connector.last_seen_at) if connector.last_seen_at else None
        online = bool(
            connector.status == "active"
            and last_seen is not None
            and now - last_seen <= CONNECTOR_OFFLINE_AFTER
        )
        result.append(
            {
                "id": int(connector.id),
                "name": connector.name,
                "status": connector.status,
                "online": online,
                "client_version": connector.client_version or "",
                "local_proxy_label": connector.local_proxy_label or "",
                "last_seen_at": connector.last_seen_at,
            }
        )
    return result


def list_online_proxy_connectors(db: Session) -> list[dict[str, object]]:
    """扫码页面只显示当前在线连接器名称，不暴露设备或代理凭证。"""

    return [item for item in list_proxy_connectors(db) if bool(item["online"])]
