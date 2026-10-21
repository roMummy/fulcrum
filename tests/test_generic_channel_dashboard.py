from __future__ import annotations

import re

import app.main as main_module

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, MessageActivityHourly
from app.core.time import hour_bucket_shanghai
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
                    api_base="http://127.0.0.1:9064",
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


def test_dashboard_with_hourly_messages_renders_real_trend(provider_page_context) -> None:
    """有小时统计时也应正常渲染；空库无法覆盖日志中引用已删除环境变量的分支。"""

    client, ids = provider_page_context
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        db.add(MessageActivityHourly(
            bot_id=ids["bot"],
            bucket_start=hour_bucket_shanghai(),
            direction="in",
            message_count=37,
        ))
        db.commit()
        # 同时检查返回的聚合值，避免仅靠 HTTP 200 掩盖趋势数据被跳过的问题。
        series = main_module.build_dashboard_trend_series(db, hours=24)
        assert series["inbound_chart"]["max_value"] == 37

    _login(client, "admin")
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "入站趋势" in response.text
