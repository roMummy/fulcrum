from __future__ import annotations

from app.api.open_api import _build_open_message_payload
from app.models.entities import Conversation


def _conversation() -> Conversation:
    return Conversation(
        id=1,
        bot_id=1,
        channel_id=2,
        channel_type="wechat_ipad",
        external_id="room@chatroom",
        title="测试群",
        chat_type="group",
    )


def test_open_message_payload_supports_874_media_message_types() -> None:
    voice = _build_open_message_payload(
        conversation=_conversation(),
        payload={"message_type": "voice", "voice_base64": "abc", "voice_time": 2},
        message_type="voice",
    )
    assert voice["message_type"] == "voice"
    assert voice["voice_base64"] == "abc"

    video = _build_open_message_payload(
        conversation=_conversation(),
        payload={"message_type": "video", "video_base64": "video", "cover_base64": "cover"},
        message_type="video",
    )
    assert video["message_type"] == "video"
    assert video["cover_base64"] == "cover"

