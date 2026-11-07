from __future__ import annotations

import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.models.entities import Bot, Channel
from app.models.saas import PlatformProxy, Tenant
from app.services.local_proxy_connector import create_proxy_connector, consume_activation_code, issue_activation_code
from app.services.login_proxy_pool import create_login_proxy_session, save_platform_proxy
from app.services.wechat_ipad.login_flow import (
    poll_login_with_proxy_session,
    start_login_with_proxy_session,
)


@pytest.fixture()
def db(tmp_path):
    """登录编排测试使用独立数据库，确保代理租约和渠道运行态互不污染。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'wechat-ipad-login-proxy-flow.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_channel(db: Session) -> tuple[Tenant, Channel]:
    """创建一条最小微信 iPad 草稿渠道。"""

    tenant = Tenant(code="proxy-flow", name="代理登录编排企业", status="active")
    db.add(tenant)
    db.flush()
    bot = Bot(tenant_id=tenant.id, name="代理登录机器人", status="stopped", is_enabled=False)
    db.add(bot)
    db.flush()
    channel = Channel(
        tenant_id=tenant.id,
        bot_id=bot.id,
        name="代理登录微信",
        channel_type="wechat_ipad",
        api_base="http://127.0.0.1:9064",
        adapter_config_json="{}",
        runtime_info_json="{}",
        status="draft",
        is_enabled=False,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return tenant, channel


class _RotatingQrClient:
    """前两个代理取码失败，第三个代理返回可展示二维码。"""

    def __init__(self):
        self.proxy_hosts: list[str] = []

    def get_qr_code(self, channel: Channel, **kwargs):
        del channel
        proxy = kwargs.get("proxy") or {}
        self.proxy_hosts.append(str(proxy.get("host") or ""))
        if len(self.proxy_hosts) < 3:
            return {"Success": False, "Code": -1, "Message": "代理连接失败", "http_status": 502}
        return {
            "Success": True,
            "Code": 1,
            "Message": "成功",
            "Data": {
                "Uuid": "proxy-flow-uuid",
                "QrUrl": "https://qr.example.com/proxy-flow-uuid",
                "ExpiredTime": 240,
            },
            "http_status": 200,
        }

    def check_qr(self, channel: Channel, *, uuid: str):
        del channel, uuid
        return {
            "Success": True,
            "Code": 0,
            "Message": "登录成功",
            "Data": {"status": 2, "Wxid": "wxid_proxy_flow", "NickName": "代理登录账号"},
            "http_status": 200,
        }


class _LocalConnectorLoginClient:
    """模拟本地连接器完成扫码后，验证编排层会清空 874 代理。"""

    def __init__(self):
        self.set_proxy_calls: list[dict[str, object]] = []

    def get_qr_code(self, channel: Channel, **kwargs):
        del channel, kwargs
        return {
            "Success": True,
            "Code": 1,
            "Data": {"Uuid": "local-connector-uuid", "QrUrl": "https://qr.example.com/local", "ExpiredTime": 240},
            "http_status": 200,
        }

    def check_qr(self, channel: Channel, *, uuid: str):
        del channel, uuid
        return {
            "Success": True,
            "Code": 0,
            "Message": "登录成功",
            "Data": {"status": 2, "Wxid": "wxid_local_connector", "NickName": "本地代理账号"},
            "http_status": 200,
        }

    def set_proxy(self, channel: Channel, *, wxid: str, proxy: dict[str, object]):
        del channel
        self.set_proxy_calls.append({"wxid": wxid, "proxy": proxy})
        return {"Success": True, "Code": 0}

    @staticmethod
    def is_success(payload: dict[str, object]) -> bool:
        return payload.get("Success") is True and payload.get("Code") == 0

def test_pool_login_rotates_before_qrcode_and_releases_after_success(db: Session) -> None:
    """取码前最多轮换三个同地区代理，二维码成功后固定并在登录完成时释放。"""

    tenant, channel = _seed_channel(db)
    proxies = [
        save_platform_proxy(
            db,
            proxy_id=None,
            values={
                "name": f"杭州代理{index}",
                "province": "浙江省",
                "city": "杭州市",
                "host": f"10.30.0.{index}",
                "port": 1080,
                "username": "proxy-user",
                "password": "proxy-password",
            },
        )
        for index in range(1, 4)
    ]
    client = _RotatingQrClient()

    with tenant_scope(tenant.id):
        session = create_login_proxy_session(
            db,
            channel=channel,
            mode="platform_pool",
            values={"province": "浙江省", "city": "杭州市"},
        )
        overview = start_login_with_proxy_session(db, channel, client=client)

    db.refresh(session)
    for proxy in proxies:
        db.refresh(proxy)
    assert overview["qrcode_status"] == "waiting_scan"
    assert client.proxy_hosts == ["10.30.0.1", "10.30.0.2", "10.30.0.3"]
    assert session.state == "qrcode_ready"
    assert session.attempt_count == 3
    assert proxies[0].lease_key is None
    assert proxies[1].lease_key is None
    assert proxies[2].lease_key == session.session_key

    with tenant_scope(tenant.id):
        completed = poll_login_with_proxy_session(db, channel, client=client)

    db.refresh(session)
    db.refresh(proxies[2])
    assert completed["login_state"] == "online"
    assert session.state == "completed"
    assert proxies[2].lease_key is None


def test_login_orchestrator_does_not_accept_client_proxy_or_tenant_fields() -> None:
    """编排入口只接受数据库会话和渠道，客户端不能临时指定代理或租户归属。"""

    parameters = inspect.signature(start_login_with_proxy_session).parameters
    assert "proxy" not in parameters
    assert "proxy_id" not in parameters
    assert "tenant_id" not in parameters


def test_local_connector_login_clears_874_proxy_before_finishing_session(db: Session) -> None:
    """本地代理只覆盖扫码登录，874 清理成功后连接器会话才进入终态。"""

    tenant, channel = _seed_channel(db)
    client = _LocalConnectorLoginClient()
    with tenant_scope(tenant.id):
        connector = create_proxy_connector(db, name="Windows 连接器")
        activation_code = issue_activation_code(db, connector_id=connector.id)
        activation = consume_activation_code(db, code=activation_code, device_id="windows-local-01")
        session = create_login_proxy_session(
            db,
            channel=channel,
            mode="local_connector",
            values={"connector_id": int(activation["connector_id"])},
        )
        start_login_with_proxy_session(db, channel, client=client)
        overview = poll_login_with_proxy_session(db, channel, client=client)

    db.refresh(session)
    assert overview["login_state"] == "online"
    assert client.set_proxy_calls == [{"wxid": "wxid_local_connector", "proxy": {}}]
    assert session.proxy_cleanup_state == "completed"
    assert session.state == "completed"
