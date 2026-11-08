from __future__ import annotations

from sqlalchemy import select

from app.models.entities import OpenApiApp
from app.models.saas import TenantSubscription
from test_provider_node_pages import _login, provider_page_context


def test_tenant_admin_can_create_and_rotate_open_api_app(provider_page_context) -> None:
    """企业管理员可以自助创建开放应用，密钥创建和轮换只展示一次。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "admin")
    created = client.post(
        "/tenant/open-apps",
        data={
            "csrf_token": csrf_token,
            "name": "订单系统",
            "app_key": "order-system",
            "default_channel_id": ids["channel_id"],
            "authorized_channel_ids": [ids["channel_id"]],
        },
    )

    assert created.status_code == 201
    assert "开放应用已创建" in created.text
    assert "请立即保存开放应用密钥" in created.text
    with ids["factory"]() as db:
        app = db.scalar(select(OpenApiApp).where(OpenApiApp.app_key == "order-system"))
        assert app is not None
        app_id = int(app.id)
        old_hash = app.app_secret_hash

    rotated = client.post(
        f"/tenant/open-apps/{app_id}/rotate-secret",
        data={"csrf_token": csrf_token},
    )
    assert rotated.status_code == 201
    assert "密钥已轮换" in rotated.text
    with ids["factory"]() as db:
        app = db.get(OpenApiApp, app_id)
        assert app is not None
        assert app.app_secret_hash != old_hash


def test_non_admin_cannot_manage_open_api_apps(provider_page_context) -> None:
    """运营和只读角色不能创建或修改开放应用密钥。"""

    client, ids = provider_page_context
    csrf_token = _login(client, "operator")
    assert client.get("/tenant/open-apps").status_code == 403
    response = client.post(
        "/tenant/open-apps",
        data={
            "csrf_token": csrf_token,
            "name": "越权应用",
            "app_key": "forbidden",
            "default_channel_id": ids["channel_id"],
        },
    )
    assert response.status_code == 403


def test_expired_subscription_blocks_open_api_app_creation(provider_page_context) -> None:
    """订阅失效后不能继续新增开放应用。"""

    client, ids = provider_page_context
    with ids["factory"]() as db:
        subscription = db.scalar(select(TenantSubscription).where(TenantSubscription.tenant_id == ids["alpha"]))
        assert subscription is not None
        subscription.status = "expired"
        db.commit()

    csrf_token = _login(client, "admin")
    response = client.post(
        "/tenant/open-apps",
        data={
            "csrf_token": csrf_token,
            "name": "已过期应用",
            "app_key": "expired-app",
            "default_channel_id": ids["channel_id"],
        },
    )
    assert response.status_code == 303
    assert "订阅已失效" in response.headers["location"]
