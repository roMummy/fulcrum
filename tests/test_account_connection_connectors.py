from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.models.entities import Bot, Channel
from app.models.saas import ProviderNode, ProviderNodeAccount, Tenant
from app.services.account_connections.telegram import TelegramAccountConnector
from app.services.account_connections.wechat_ipad import WechatIpadAccountConnector
from app.services.account_connections.wechat_winauto import WechatWinAutoAccountConnector
from app.services.account_connections.workwechat import WorkWechatAccountConnector
from app.services.channel_adapters.telegram import TelegramAdapter
from app.services.channel_adapters.wechat_winauto import WechatWinAutoAdapter


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'account-connectors.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_channel(db: Session, *, channel_type: str) -> tuple[Tenant, Channel]:
    tenant = Tenant(code=f"tenant-{channel_type}", name="连接测试企业", status="active")
    db.add(tenant)
    db.flush()
    bot = Bot(tenant_id=tenant.id, name=f"{channel_type} Bot", status="stopped", is_enabled=False)
    db.add(bot)
    db.flush()
    channel = Channel(
        tenant_id=tenant.id,
        bot_id=bot.id,
        name=f"{channel_type} 渠道",
        channel_type=channel_type,
        status="draft",
        is_enabled=False,
    )
    db.add(channel)
    db.commit()
    return tenant, channel


def test_telegram_connector_uses_verified_bot_id(db: Session, monkeypatch) -> None:
    tenant, channel = _seed_channel(db, channel_type="telegram")
    monkeypatch.setattr(
        TelegramAdapter,
        "health_check",
        lambda self, target: {
            "ok": True,
            "status": "online",
            "summary": "ok",
            "details": {"bot_id": 10001, "username": "sales_bot"},
        },
    )

    with tenant_scope(tenant.id):
        result = TelegramAccountConnector().verify(db, channel)

    assert result.state == "verified"
    assert result.identity.account_id == "10001"
    assert result.identity.device_key == "telegram-bot:10001"


def test_workwechat_connector_uses_online_discovered_account(db: Session) -> None:
    tenant, channel = _seed_channel(db, channel_type="workwechat")
    node = ProviderNode(
        tenant_id=tenant.id,
        name="客服电脑",
        status="active",
        device_id="device-001",
    )
    db.add(node)
    db.flush()
    account = ProviderNodeAccount(
        tenant_id=tenant.id,
        provider_node_id=node.id,
        account_key="account-key",
        external_user_id="zhangsan",
        display_name="张三",
        status="online",
    )
    db.add(account)
    db.flush()
    channel.provider_node_id = node.id
    channel.provider_account_id = account.id
    db.commit()

    with tenant_scope(tenant.id):
        result = WorkWechatAccountConnector().verify(db, channel)

    assert result.state == "verified"
    assert result.identity.account_id == "zhangsan"
    assert result.identity.device_key == "workwechat-device:device-001"


def test_wechat_ipad_connector_waits_for_login_and_reads_protocol_identity(db: Session) -> None:
    tenant, channel = _seed_channel(db, channel_type="wechat_ipad")
    with tenant_scope(tenant.id):
        connecting = WechatIpadAccountConnector().verify(db, channel)
    assert connecting.state == "connecting"

    channel.login_state = "online"
    channel.adapter_config_json = json.dumps(
        {"wxid": "wxid_001", "device_id": "ipad-device-001", "nickname": "小曜"},
        ensure_ascii=False,
    )
    db.commit()
    with tenant_scope(tenant.id):
        verified = WechatIpadAccountConnector().verify(db, channel)

    assert verified.state == "verified"
    assert verified.identity.account_id == "wxid_001"
    assert verified.identity.display_name == "小曜"


def test_wechat_winauto_connector_requires_client_identity(db: Session, monkeypatch) -> None:
    tenant, channel = _seed_channel(db, channel_type="wechat_winauto")
    monkeypatch.setattr(
        WechatWinAutoAdapter,
        "health_check",
        lambda self, target: {
            "ok": True,
            "status": "online",
            "details": {
                "response": {
                    "data": {
                        "account_id": "wx_windows_001",
                        "device_id": "windows-device-001",
                        "display_name": "Windows 微信",
                    }
                }
            },
        },
    )

    with tenant_scope(tenant.id):
        result = WechatWinAutoAccountConnector().verify(db, channel)

    assert result.state == "verified"
    assert result.identity.account_id == "wx_windows_001"
    assert result.identity.device_key == "wechat-windows-device:windows-device-001"
