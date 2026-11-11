"""bot 退群/被移出后的界面:群列表显示"已退出"、没有启用开关、可单独筛选、排在最后;群详情显示说明。"""

from __future__ import annotations

import os
import re
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-group-left-pages-logs-"))

import pytest

from app.models.entities import Channel, Chatroom
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)


@pytest.fixture()
def pages(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.flush()
        rows = {}
        for key, name, status, mode, active in (
            ("live", "在群的群", "synced", "enabled", True),
            ("left", "退出的群", "left", "enabled", False),
            ("off", "关闭的群", "synced", "archived", False),
        ):
            room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, external_id=f"{key}1@chatroom", name=name, status=status, management_status=mode, is_active=active, member_count=3)
            db.add(room)
            db.flush()
            rows[key] = int(room.id)
        db.commit()
        return client, int(channel.id), rows


def _tr(html: str, name: str) -> str:
    """取出包含某个群名的那一行 <tr>。"""

    start = html.index(name)
    begin = html.rfind("<tr>", 0, start)
    return html[begin: html.index("</tr>", start)]


def test_the_group_list_marks_left_groups_and_removes_their_switch(pages) -> None:
    client, _channel_id, _rows = pages
    _login(client, "admin")
    html = client.get("/chatrooms").text
    left, live = _tr(html, "退出的群"), _tr(html, "在群的群")
    assert "已退出" in left and 'role="switch"' not in left and "设置" in left  # 没有启用开关,仍可进入详情
    assert "已退出" not in live and 'role="switch"' in live
    assert html.index("退出的群") > html.index("在群的群") and html.index("退出的群") > html.index("关闭的群")  # 已退出的排在最后
    assert '<option value="left"' in html


def test_the_left_filter_only_shows_left_groups_and_other_filters_hide_them(pages) -> None:
    client, _channel_id, _rows = pages
    _login(client, "admin")
    only_left = client.get("/chatrooms?status=left").text
    assert "退出的群" in only_left and "在群的群" not in only_left and "关闭的群" not in only_left
    running = client.get("/chatrooms?status=enabled").text
    assert "在群的群" in running and "退出的群" not in running  # 已退出的群不算"正常运行"


def test_the_group_detail_explains_that_the_bot_is_no_longer_in_the_group(pages) -> None:
    client, channel_id, rows = pages
    _login(client, "admin")
    left = client.get(f"/channels/{channel_id}/chatrooms/{rows['left']}").text
    assert "机器人已不在这个群里" in left and "重新加入后会自动恢复" in left and re.search(r'badge-error[^>]*>已退出<', left) and "原设置：正常运行" in left
    live = client.get(f"/channels/{channel_id}/chatrooms/{rows['live']}").text
    assert "机器人已不在这个群里" not in live
