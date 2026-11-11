"""已经在线、没有接入 Token 的老微桥渠道：合入 Token 校验后的升级路径。

背景：Token 校验只在「新建接入」时生成 Token，而线上已有的渠道从来没有 Token，旧的不带 Token 地址会被拒绝。
管理员打开连接页时必须能为老渠道生成 Token 并看到完整地址，手机换成新地址后才能继续工作。
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from app.models.entities import Channel
from app.services.wechat_webridge.access import connection_url
from test_provider_node_pages import provider_page_context  # noqa: F401  (fixture)
from test_webridge_connection_flow import IDENTITY, PUBLIC_BASE, bridge_context, isolate_bridge_runtime  # noqa: F401  (含 fixture)


@pytest.fixture()
def legacy_channel(bridge_context):  # noqa: F811
    client, ids, csrf = bridge_context
    with ids["factory"]() as db:
        channel = Channel(
            tenant_id=ids["alpha"], bot_id=ids["bot"], name="老微桥", channel_type="wechat_webridge",
            status="online", is_enabled=True, api_base=None,
        )
        db.add(channel)
        db.flush()
        channel.api_base = f"{PUBLIC_BASE}/api/wechat-webridge/{channel.id}"  # 老渠道的地址:没有 Token
        db.commit()
        return client, ids, csrf, int(channel.id)


def test_a_legacy_channel_gets_a_token_when_the_admin_opens_the_connection_page(legacy_channel) -> None:
    client, ids, _csrf, channel_id = legacy_channel
    with ids["factory"]() as db:
        assert connection_url(db.get(Channel, channel_id)) == ""  # 起点:确实没有 Token

    old_url = f"/api/wechat-webridge/{channel_id}/agent/poll"
    assert client.post(old_url, json=IDENTITY).status_code in (404, 405)  # 旧的不带 Token 地址不再被接受
    assert client.post(f"/api/wechat-webridge/{channel_id}/wrong-token/agent/poll", json=IDENTITY).status_code == 403

    page = client.get(f"/channels/connect?channel_id={channel_id}")
    assert page.status_code == 200
    with ids["factory"]() as db:
        address = connection_url(db.get(Channel, channel_id))
    assert address.startswith(f"{PUBLIC_BASE}/api/wechat-webridge/{channel_id}/") and len(address.rsplit("/", 1)[1]) >= 32
    assert address in page.text  # 页面上能看到并复制完整地址
    assert page.headers["cache-control"] == "no-store"

    again = client.get(f"/channels/connect?channel_id={channel_id}")
    assert address in again.text  # 再次打开地址不变,不会每次换 Token 把手机弄断

    path = urlsplit(address).path
    assert client.post(path + "/agent/register", json=IDENTITY).status_code == 200  # 手机换上新地址后能连上
    assert client.post(path + "/agent/poll", json=IDENTITY).status_code == 200
    with ids["factory"]() as db:
        assert db.get(Channel, channel_id).is_enabled is True  # 老渠道一直是启用状态,没被网页确认流程重置


def test_only_the_tenant_admin_can_see_a_legacy_channels_address(legacy_channel) -> None:
    client, ids, _csrf, channel_id = legacy_channel
    from test_provider_node_pages import _login

    for role in ("operator", "readonly"):
        _login(client, role)
        assert client.get(f"/channels/connect?channel_id={channel_id}").status_code in (303, 403)
    with ids["factory"]() as db:
        assert connection_url(db.get(Channel, channel_id)) == ""  # 非管理员打开也不会生成或泄露 Token
