from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel
from app.models.saas import (
    ProxyConnector,
    ProxyConnectorActivationCode,
    ProxyConnectorCredential,
    Tenant,
)
from app.services.local_proxy_connector import (
    LocalProxyConnectorError,
    authenticate_device,
    consume_activation_code,
    create_proxy_connector,
    issue_activation_code,
    list_online_proxy_connectors,
)


@pytest.fixture()
def db(tmp_path):
    """连接器服务测试使用独立 SQLite，避免真实租户和设备密钥互相污染。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'local-proxy-connector.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_tenant(db: Session, code: str = "connector-tenant") -> Tenant:
    """创建最小租户和渠道，验证连接器实体能够进入统一 SaaS 元数据。"""

    tenant = Tenant(code=code, name=f"{code} 企业", status="active")
    db.add(tenant)
    db.flush()
    bot = Bot(tenant_id=tenant.id, name=f"{code} 机器人", status="stopped", is_enabled=False)
    db.add(bot)
    db.flush()
    db.add(
        Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name=f"{code} 微信",
            channel_type="wechat_ipad",
            api_base="http://127.0.0.1:9064",
            status="draft",
            is_enabled=False,
        )
    )
    db.commit()
    return tenant


def test_activation_code_is_one_time_and_device_secret_is_not_persisted(db: Session) -> None:
    """激活码只能消费一次，设备密钥原文只返回给连接器而不写入任何表。"""

    tenant = _seed_tenant(db)
    with tenant_scope(tenant.id):
        connector = create_proxy_connector(db, name="办公室代理")
        code = issue_activation_code(db, connector_id=connector.id)
        result = consume_activation_code(
            db,
            code=code,
            device_id="windows-device-01",
            client_version="1.0.0",
            local_proxy_label="本机 SOCKS5",
        )

    assert result["connector_id"] == connector.id
    assert result["device_key"]
    assert db.scalar(select(ProxyConnectorActivationCode).where(ProxyConnectorActivationCode.connector_id == connector.id)).consumed_at
    assert db.scalar(select(ProxyConnectorCredential).where(ProxyConnectorCredential.connector_id == connector.id)).device_key_hash != result["device_key"]
    assert db.scalar(select(ProxyConnector).where(ProxyConnector.id == connector.id)).device_id == "windows-device-01"

    with tenant_scope(tenant.id):
        with pytest.raises(LocalProxyConnectorError, match="connector_activation_code_invalid"):
            consume_activation_code(db, code=code, device_id="windows-device-02")


def test_device_authentication_refreshes_online_summary_without_proxy_secret(db: Session) -> None:
    """设备认证只刷新在线状态，页面摘要不包含设备密钥或本地代理密码。"""

    tenant = _seed_tenant(db, code="connector-online")
    with tenant_scope(tenant.id):
        connector = create_proxy_connector(db, name="家用代理")
        code = issue_activation_code(db, connector_id=connector.id)
        result = consume_activation_code(db, code=code, device_id="windows-device-02")
        authenticated = authenticate_device(db, device_key=str(result["device_key"]))
        rows = list_online_proxy_connectors(db)

    assert authenticated.id == connector.id
    assert rows[0]["online"] is True
    assert "device_key" not in rows[0]
    assert "password" not in str(rows[0]).lower()


def test_expired_activation_code_is_rejected(db: Session) -> None:
    """过期激活码不能绕过有效期绑定设备。"""

    tenant = _seed_tenant(db, code="connector-expired")
    with tenant_scope(tenant.id):
        connector = create_proxy_connector(db, name="过期连接器")
        # 直接构造摘要记录模拟过期，避免在测试中等待真实时间流逝。
        db.add(
            ProxyConnectorActivationCode(
                tenant_id=tenant.id,
                connector_id=connector.id,
                code_hash=hashlib.sha256(b"expired-code").hexdigest(),
                expires_at=now_shanghai() - timedelta(seconds=1),
            )
        )
        db.commit()
        with pytest.raises(LocalProxyConnectorError, match="connector_activation_code_expired"):
            consume_activation_code(db, code="expired-code", device_id="windows-device-03")
