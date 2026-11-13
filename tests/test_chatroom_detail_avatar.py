"""群详情沿用群目录里已保存的头像，无头像时保留首字占位。"""

from __future__ import annotations

import json
import os
import re
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="chatroom-detail-avatar-logs-"))

import pytest

from app.models.entities import Channel, Chatroom
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


def _create_room(ids, channel_type: str, extra_json: str | None) -> tuple[int, int]:
    with ids["factory"]() as db:
        channel = Channel(
            tenant_id=ids["alpha"], bot_id=ids["bot"], name="群头像测试账号",
            channel_type=channel_type, is_enabled=True,
        )
        db.add(channel)
        db.flush()
        room = Chatroom(
            tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id,
            external_id=f"avatar-{channel_type}@chatroom", name="测试群",
            extra_json=extra_json,
        )
        db.add(room)
        db.commit()
        return channel.id, room.id


def _header_avatar(html: str) -> str:
    return re.search(r'<span class="identity-avatar">(.*?)</span>\s*<span class="identity-text">', html, re.S).group(1)


@pytest.mark.parametrize("channel_type,avatar_key", [
    ("wechat_ipad", "avatar_url"),
    ("wechat_winauto", "avatar"),
    ("workwechat", "head_img_url"),
    ("wechat_webridge", "avatar_url"),
])
def test_group_detail_keeps_the_avatar_shown_in_the_directory(provider_page_context, channel_type, avatar_key):
    client, ids = provider_page_context
    avatar_url = "https://example.com/group-avatar.png"
    channel_id, room_id = _create_room(ids, channel_type, json.dumps({avatar_key: avatar_url}))
    _login(client, "admin")

    listing = client.get(f"/chatrooms?channel_id={channel_id}")
    detail = client.get(f"/channels/{channel_id}/chatrooms/{room_id}")

    assert listing.status_code == detail.status_code == 200
    assert f'src="{avatar_url}"' in listing.text
    assert f'src="{avatar_url}"' in _header_avatar(detail.text)


@pytest.mark.parametrize("extra_json", [None, "{invalid", "[]", '{"avatar_url": ""}'])
def test_group_detail_without_an_avatar_keeps_the_initial_placeholder(provider_page_context, extra_json):
    client, ids = provider_page_context
    channel_id, room_id = _create_room(ids, "wechat_webridge", extra_json)
    _login(client, "admin")

    response = client.get(f"/channels/{channel_id}/chatrooms/{room_id}")

    assert response.status_code == 200
    assert _header_avatar(response.text) == "<span>测</span>"
