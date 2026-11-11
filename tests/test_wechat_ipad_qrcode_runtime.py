from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# 应用服务在导入阶段初始化文件日志；测试必须使用工作区内的隔离目录，不能写生产日志路径。
TEST_LOG_DIR = Path(__file__).resolve().parents[1] / ".run" / "test-runtime" / "wechat-ipad-qrcode-runtime-logs"
TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
os.environ["LOG_DIR"] = str(TEST_LOG_DIR)

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.models.entities import Bot, Channel
from app.models.saas import Tenant
from app.services.wechat_ipad.service import resolve_wechat_ipad_qrcode_image, start_wechat_ipad_login


@pytest.fixture()
def db(tmp_path):
    """使用独立数据库验证二维码运行状态，不加载历史演示数据和旧测试夹具。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'wechat-ipad-qrcode-runtime.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_ipad_channel(db: Session) -> tuple[Tenant, Channel]:
    """创建最小租户、机器人和 iPad 渠道，字段与真实草稿保持一致。"""

    tenant = Tenant(code="ipad-qrcode-runtime", name="二维码运行态测试企业", status="active")
    db.add(tenant)
    db.flush()
    bot = Bot(
        tenant_id=tenant.id,
        name="二维码运行态测试机器人",
        status="stopped",
        is_enabled=False,
    )
    db.add(bot)
    db.flush()
    channel = Channel(
        tenant_id=tenant.id,
        bot_id=bot.id,
        name="iPad 874 二维码测试",
        channel_type="wechat_ipad",
        api_base="http://192.168.1.240:9064",
        adapter_config_json="{}",
        runtime_info_json="{}",
        status="draft",
        is_enabled=False,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return tenant, channel


class _LargeQrClient:
    """模拟 240 上 874 的真实回包，同时携带短链接和约 66 KB Base64 图片。"""

    def __init__(self, qr_base64: str, qr_url: str):
        self.qr_base64 = qr_base64
        self.qr_url = qr_url
        self.last_proxy = None

    def get_qr_code(self, channel: Channel, **kwargs):
        del channel
        self.last_proxy = kwargs.get("proxy")
        return {
            "Success": True,
            "Code": 1,
            "Message": "成功",
            "Data": {
                "Uuid": "uuid-large-runtime-001",
                "QrUrl": self.qr_url,
                "QrBase64": f"data:image/jpeg;base64,{self.qr_base64}",
                "ExpiredTime": "2026-08-29 19:10:00",
            },
            "DeviceId": "49f67a9d4c7e4b4f9a5c3d2e1f0a8741",
            "http_status": 200,
        }


def test_wechat_ipad_qrcode_runtime_omits_large_base64_and_keeps_short_url(db: Session) -> None:
    """真实量级二维码只能通过短链接展示，禁止把重复图片载荷写入渠道运行状态。"""

    tenant, channel = _seed_ipad_channel(db)
    # 240 上的真实二维码 Base64 约 66 KB；使用同量级数据可稳定复现 MariaDB TEXT 溢出。
    qr_base64 = base64.b64encode(b"q" * 50000).decode("ascii")
    qr_url = "https://qr.example.com/uuid-large-runtime-001"

    with tenant_scope(tenant.id):
        overview = start_wechat_ipad_login(
            db,
            channel,
            login_profile="ipad",
            client=_LargeQrClient(qr_base64, qr_url),
        )

    runtime_json = channel.runtime_info_json or "{}"
    runtime_info = json.loads(runtime_json)
    self_image = resolve_wechat_ipad_qrcode_image(channel)

    assert len(runtime_json.encode("utf-8")) < 65535
    assert runtime_info.get("login_qr_uuid") == "uuid-large-runtime-001"
    assert runtime_info.get("qrcode_url") == qr_url
    assert "qrcode_image_base64" not in runtime_info
    assert qr_base64 not in runtime_json
    assert runtime_info.get("last_login_init_response", {}).get("message") == "成功"
    assert runtime_info.get("last_login_init_response", {}).get("success") is True
    assert overview["qrcode_image_url"] == f"/channels/{channel.id}/wechat-ipad/qrcode-image"
    assert overview["protocol_state"]["last_get_qr_result"]["state"] == "healthy"
    assert self_image == {"available": True, "kind": "url", "url": qr_url}


def test_wechat_ipad_qrcode_runtime_rejects_base64_only_response(db: Session) -> None:
    """缺少二维码短链接时必须明确失败，不恢复已废弃的 Base64 展示分支。"""

    tenant, channel = _seed_ipad_channel(db)
    qr_base64 = base64.b64encode(b"q" * 50000).decode("ascii")

    with tenant_scope(tenant.id):
        overview = start_wechat_ipad_login(
            db,
            channel,
            login_profile="ipad",
            client=_LargeQrClient(qr_base64, ""),
        )

    runtime_json = channel.runtime_info_json or "{}"
    runtime_info = json.loads(runtime_json)

    assert channel.login_state == "login_init_failed"
    assert channel.qrcode_status == "failed"
    assert overview["last_error"] == "qrcode_url_missing"
    assert overview["qrcode_image_url"] is None
    assert runtime_info.get("last_login_init_response", {}).get("message") == "成功"
    assert len(runtime_json.encode("utf-8")) < 65535
    assert qr_base64 not in runtime_json
    assert resolve_wechat_ipad_qrcode_image(channel) == {"available": False}


def test_wechat_ipad_qrcode_runtime_uses_explicit_login_proxy(db: Session) -> None:
    """874 取码只使用调用方显式传入的临时代理，不再读取渠道适配配置。"""

    tenant, channel = _seed_ipad_channel(db)
    channel.adapter_config_json = json.dumps(
        {"proxy": {"host": "old-proxy", "port": "9999", "password": "old-secret"}},
        ensure_ascii=False,
    )
    db.commit()
    client = _LargeQrClient("", "https://qr.example.com/explicit-proxy")
    explicit_proxy = {
        "host": "127.0.0.1",
        "port": "1080",
        "username": "login-user",
        "password": "login-password",
    }

    with tenant_scope(tenant.id):
        start_wechat_ipad_login(
            db,
            channel,
            login_profile="ipad",
            proxy=explicit_proxy,
            client=client,
        )

    assert client.last_proxy == explicit_proxy
