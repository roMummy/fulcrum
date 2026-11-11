"""微桥媒体上传接口:身份在 multipart 表单里,成功响应必须带 asset_id。"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-upload-test-logs-"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import wechat_webridge as api


class _Db:
    def __init__(self, attachment):
        self.attachment = attachment
        self.committed = False

    def get(self, model, key):
        return SimpleNamespace(id=2)

    def scalar(self, stmt):
        return self.attachment

    def commit(self):
        self.committed = True


@pytest.fixture()
def client(monkeypatch):
    attachment = SimpleNamespace(id=95, channel_id=2)
    db = _Db(attachment)
    seen: dict = {}

    class _Ctx:
        def __enter__(self):
            return db

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(api, "_channel_tenant", lambda cid: (1, cid))
    monkeypatch.setattr(api, "tenant_scope", lambda tid: _Ctx())
    monkeypatch.setattr(api, "tenant_session", lambda factory: _Ctx())

    def fake_identity(channel, body):
        seen.update(body)
        return body.get("device_id") == "dev-1" and body.get("session_id") == "s-1" and body.get("account_id") == "wxid_a"

    monkeypatch.setattr(api, "identity_matches", fake_identity)
    monkeypatch.setattr(
        api,
        "store_uploaded_media_attachment",
        lambda db, **kw: {"attachment_id": 95, "storage_path": "/x/95.mp4", "file_size": len(kw["content"]), "mime_type": kw["content_type"]},
    )
    app = FastAPI()
    # 路由凭据由接入流程集成测试覆盖；本文件专门核对 multipart 身份和媒体处理。
    app.dependency_overrides[api._require_device_token] = lambda: None
    app.include_router(api.router)
    test_client = TestClient(app)
    test_client.seen = seen
    return test_client


def test_identity_is_read_from_form_fields_and_response_has_asset_id(client) -> None:
    response = client.post(
        "/api/wechat-webridge/2/test-connection/agent/upload_media?attachment_id=95",
        data={"device_id": "dev-1", "session_id": "s-1", "account_id": "wxid_a", "media_type": "video"},
        files={"file": ("video.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["asset_id"] == "95"


def test_wrong_identity_is_rejected(client) -> None:
    response = client.post(
        "/api/wechat-webridge/2/test-connection/agent/upload_media?attachment_id=95",
        data={"device_id": "other", "session_id": "s-1", "account_id": "wxid_a"},
        files={"file": ("a.mp4", b"abc", "video/mp4")},
    )
    assert response.status_code == 409


@pytest.mark.parametrize("configured", ["", "/agent/upload_media", "https://legacy.example/api/wechat-webridge/2/agent/upload_media"])
def test_generated_upload_address_takes_precedence_over_legacy_config(configured) -> None:
    """旧账号生成专属地址后，媒体上传不能继续使用无 Token 的配置。"""

    import json

    from app.models.entities import Channel
    from app.services.wechat_webridge.access import connection_url, initialize_connection
    from app.services.wechat_webridge.service import build_upload_url

    channel = Channel(id=2, bot_id=1, tenant_id=1, name="测试微桥", channel_type="wechat_webridge", adapter_config_json=json.dumps({"upload_url": configured}))
    initialize_connection(channel, "https://bridge.example")
    assert build_upload_url(channel, 95) == connection_url(channel) + "/agent/upload_media?attachment_id=95"
