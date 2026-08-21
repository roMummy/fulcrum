from __future__ import annotations

import re

import app.main as main_module

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel
from test_provider_node_pages import _login, provider_page_context


def test_dashboard_uses_generic_channel_status_and_copy(provider_page_context) -> None:
    """首页必须按统一渠道状态统计，不能要求存在企业微信账号才认为接入在线。"""

    client, ids = provider_page_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        db.add_all(
            [
                Channel(
                    bot_id=ids["bot"],
                    name="在线微信",
                    channel_type="wechat_ipad",
                    api_base="http://127.0.0.1:8848",
                    is_enabled=True,
                    status="online",
                ),
                Channel(
                    bot_id=ids["bot"],
                    name="离线 Telegram",
                    channel_type="telegram",
                    is_enabled=True,
                    status="offline",
                ),
            ]
        )
        db.commit()

    _login(client, "admin")
    response = client.get("/dashboard")
    bot_list = client.get("/bots")
    bot_detail = client.get(f"/bots/{ids['bot']}")

    assert response.status_code == 200
    assert "在线渠道" in response.text
    assert re.search(r"在线渠道</span>\s*<strong>1</strong>", response.text)
    assert re.search(r"异常渠道</span>\s*<strong>1</strong>", response.text)
    assert "在线企业微信账号" not in response.text
    assert "企业微信账号</th>" not in response.text
    assert "当前没有在线的企业微信账号" not in response.text
    assert "当前没有在线渠道" not in response.text
    assert bot_list.status_code == bot_detail.status_code == 200
    for page in (bot_list.text, bot_detail.text):
        assert "企业微信账号" not in page
        assert "渠道" in page


def test_manual_bot_creation_route_and_button_are_removed(provider_page_context) -> None:
    """机器人仍可作为内部运行对象查看，但租户不能再从页面或 POST 手工创建。"""

    client, _ids = provider_page_context
    _login(client, "admin")
    page = client.get("/bots")
    assert page.status_code == 200
    assert "新建机器人" not in page.text
    assert 'action="/bots"' not in page.text
    assert not any(
        getattr(route, "path", "") == "/bots" and "POST" in (getattr(route, "methods", None) or set())
        for route in main_module.app.routes
    )
