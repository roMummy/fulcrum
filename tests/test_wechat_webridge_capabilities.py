from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-test-logs-"))

from app.models.entities import Channel, Conversation
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.wechat_webridge import service


def test_webridge_normalizes_nested_media_and_payment_events() -> None:
    """嵌套事件必须保留消息类型、媒体元数据和支付标识，供统一事件层继续处理。"""

    channel = Channel(id=7, bot_id=1, name="微桥", channel_type="wechat_webridge")
    normalized = WechatWeBridgeAdapter().normalize_inbound_payload(
        channel,
        {
            "event_name": "message",
            "event_id": "evt-image-1",
            "body": {
                "msg_type": "image",
                "chat_id": "wxid-room",
                "from": "wxid-alice",
                "message_id": "msg-1",
                "image_md5": "md5-1",
                "image_aes_key": "aes-1",
            },
        },
    )

    assert normalized["event_key"] == "evt-image-1"
    assert normalized["message"]["message_type"] == "image"
    assert normalized["details"]["media"]["image_md5"] == "md5-1"

    payment = WechatWeBridgeAdapter().normalize_inbound_payload(
        channel,
        {"event_name": "transfer_received", "body": {"chat_id": "wxid-alice", "transfer_id": "tx-1", "amount_fen": 100}},
    )
    assert payment["message"]["message_type"] == "transfer"
    assert payment["details"]["payment"]["transfer_id"] == "tx-1"


def test_webridge_outbound_message_mapping_covers_media_and_cards(monkeypatch) -> None:
    """统一出站消息类型应转换为文档规定的手机命令和参数名。"""

    captured: list[dict] = []

    def fake_enqueue_job(db, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id=42)

    monkeypatch.setattr(service, "enqueue_job", fake_enqueue_job)
    channel = Channel(id=9, bot_id=1, tenant_id=3, name="微桥", channel_type="wechat_webridge")
    conversation = Conversation(id=11, bot_id=1, channel_id=9, external_id="wxid-bob", chat_type="direct")

    service.enqueue_message_command(
        object(), channel, conversation, {"message_type": "image", "image_url": "https://example.test/a.jpg"}
    )
    service.enqueue_message_command(
        object(), channel, conversation, {"message_type": "link", "title": "文档", "link_url": "https://example.test"}
    )

    assert captured[0]["payload"]["command"]["type"] == "send_image"
    assert captured[0]["payload"]["command"]["params"]["source"].endswith("a.jpg")
    assert captured[1]["payload"]["command"]["type"] == "send_link_card"
    assert captured[1]["payload"]["command"]["params"]["url"] == "https://example.test"


def test_webridge_media_upload_command_contains_attachment_reference(monkeypatch) -> None:
    """媒体附件任务必须携带附件 ID，手机回传时才能完成正确的幂等落库。"""

    captured: list[dict] = []

    def fake_enqueue_job(db, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id=88)

    monkeypatch.setattr(service, "enqueue_job", fake_enqueue_job)
    channel = Channel(id=4, bot_id=1, tenant_id=2, name="微桥", channel_type="wechat_webridge")
    conversation = Conversation(id=5, bot_id=1, channel_id=4, external_id="wxid-room", chat_type="group")
    service.enqueue_media_upload_command(
        object(),
        channel=channel,
        conversation=conversation,
        message_id="msg-88",
        media_type="video",
        upload_url="/api/wechat-webridge/4/agent/upload_media",
        attachment_id=99,
    )

    command = captured[0]["payload"]["command"]
    assert command["type"] == "upload_message_media"
    assert command["params"] == {
        "message_id": "msg-88",
        "media_type": "video",
        "upload_url": "/api/wechat-webridge/4/agent/upload_media",
        "attachment_id": 99,
    }


def test_webridge_command_catalog_exposes_groups_and_runtime_capabilities() -> None:
    """能力页同时提供稳定命令目录和手机注册时实际上报的能力。"""

    channel = Channel(
        id=12,
        bot_id=1,
        channel_type="wechat_webridge",
        runtime_info_json=json.dumps({"webridge": {"online": True, "capabilities": ["moments_list"]}}),
    )
    catalog = service.command_catalog(channel)
    assert "moments_list" in catalog["groups"]["moments"]
    assert catalog["reported_capabilities"] == ["moments_list"]
    assert catalog["online"] is True
