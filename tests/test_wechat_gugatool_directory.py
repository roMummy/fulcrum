"""咕嘎助手群名册缓存与账号资料回填测试。"""

from __future__ import annotations

import json

from app.models.entities import Channel
from app.services.wechat_gugatool_directory import (
    cache_group,
    cached_group,
    clear_cache,
    finish_group_refresh,
    request_group_refresh,
    save_account_profile,
)


def _channel() -> Channel:
    return Channel(
        id=1,
        bot_id=1,
        name="咕嘎助手",
        channel_type="wechat_gugatool",
        runtime_info_json=json.dumps(
            {"account_connection": {"account_id": "wxid_self", "display_name": "wxid_self"}},
            ensure_ascii=False,
        ),
    )


def test_group_cache_roundtrip_and_clear() -> None:
    clear_cache()
    cache_group(1, "1@chatroom", title="测试群", members={"wxid_a": {"name": "甲"}})

    assert cached_group(1, "1@chatroom")["title"] == "测试群"
    assert cached_group(2, "1@chatroom") is None

    clear_cache()
    assert cached_group(1, "1@chatroom") is None


def test_group_refresh_is_deduplicated_until_finished() -> None:
    clear_cache()
    assert request_group_refresh(1, "1@chatroom") is True
    assert request_group_refresh(1, "1@chatroom") is False

    finish_group_refresh(1, "1@chatroom")
    assert request_group_refresh(1, "1@chatroom") is True


def test_save_account_profile_updates_identity_and_display_name() -> None:
    channel = _channel()

    save_account_profile(channel, account_id="wxid_self", name="咕咕嘎嘎", avatar_url="https://example.com/a.jpg")

    runtime = json.loads(channel.runtime_info_json)
    assert runtime["account_profile"]["name"] == "咕咕嘎嘎"
    assert runtime["account_profile"]["avatar_url"] == "https://example.com/a.jpg"
    assert runtime["account_connection"]["display_name"] == "咕咕嘎嘎"
