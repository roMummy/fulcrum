"""咕嘎助手原生音乐卡片：XML 模板与 SendApp 入队。"""

from __future__ import annotations

import json

from app.models.entities import Channel, Conversation
from app.services.channel_adapters.wechat_gugatool import (
    WechatGugatoolAdapter,
    build_music_card_xml,
)


def _channel() -> Channel:
    return Channel(
        id=1,
        bot_id=1,
        name="咕嘎助手",
        channel_type="wechat_gugatool",
        runtime_info_json=json.dumps({"guga_agent": {"account_id": "wxid_self"}}, ensure_ascii=False),
    )


def _conversation() -> Conversation:
    return Conversation(
        id=1,
        bot_id=1,
        channel_id=1,
        channel_type="wechat_gugatool",
        external_id="123@chatroom",
        chat_type="group",
    )


def _payload() -> dict[str, str]:
    return {
        "message_type": "music",
        "title": "天下",
        "desc": "张杰",
        "url": "https://music.163.com/song?id=191254",
        "data_url": "https://m801.music.126.net/a/b.mp3",
        "song_album_url": "https://p3.music.126.net/x/y.jpg",
        "song_lyric": "[00:01.00]第一行",
        "app_id": "wx8dd6ecd81906fd84",
    }


def test_music_card_xml_uses_native_type_and_escapes_fields() -> None:
    xml = build_music_card_xml({**_payload(), "title": "A&B <晴天>", "song_lyric": "a\x00b"}, "wxid_self")
    assert xml.startswith('<appmsg appid="wx8dd6ecd81906fd84" sdkver="0">')
    assert "<type>76</type>" in xml
    assert "<title>A&amp;B &lt;晴天&gt;</title>" in xml
    assert "<dataurl>https://m801.music.126.net/a/b.mp3</dataurl>" in xml
    assert "<fromusername>wxid_self</fromusername>" in xml
    assert "\x00" not in xml
    assert xml.endswith("<commenturl/>")


def test_send_music_queues_send_app_type_three() -> None:
    adapter = WechatGugatoolAdapter()
    captured: dict = {}

    def fake_enqueue(channel, conversation, command_name, params, *, priority=90):
        captured.update(command_name=command_name, params=params)
        return "req-1", 7

    adapter._enqueue = fake_enqueue  # type: ignore[method-assign]
    result = adapter.send_message(_channel(), _conversation(), _payload())

    assert captured["command_name"] == "send_app"
    assert captured["params"]["to_wxid"] == "123@chatroom"
    assert captured["params"]["type"] == 3
    assert "<type>76</type>" in captured["params"]["xml"]
    assert "<fromusername>wxid_self</fromusername>" in captured["params"]["xml"]
    assert result["ok"] is True and result["message_type"] == "music"
    assert result["command_name"] == "send_app" and result["transport_status"] == "queued"


def test_send_music_requires_title_and_play_url() -> None:
    adapter = WechatGugatoolAdapter()
    adapter._enqueue = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not enqueue"))
    result = adapter.send_message(_channel(), _conversation(), {**_payload(), "data_url": ""})
    assert result["ok"] is False and result["error_code"] == "music_payload_missing"
