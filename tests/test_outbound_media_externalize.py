"""出站媒体不落 base64:载荷里的 base64 必须变成文件地址,调试信息里只留体积说明。"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="outbound-media-test-logs-"))

from app.services import outbound_files
from app.services.outbound_files import externalize_media_payload, redact_base64_fields

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64



class _FakeDb:
    """enqueue_job 被桩掉了;出站前会查"bot 是否已退出这个群",这里一律查不到。"""

    def scalar(self, stmt):
        return None


def test_image_base64_becomes_file_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", tmp_path)
    payload = {"message_type": "image", "base64": base64.b64encode(PNG).decode(), "conversation_id": 4}
    result = externalize_media_payload(payload, tenant_id=1, channel_id=2, public_base_url="http://host:8280/")

    assert "base64" not in result
    assert result["conversation_id"] == 4
    url = result["image_url"]
    assert url.startswith("http://host:8280/outbound-files/2/") and url.endswith("/image.png")
    # 文件真实写入租户目录,内容与原始字节一致。
    saved = list(Path(tmp_path).rglob("image.png"))
    assert len(saved) == 1 and saved[0].read_bytes() == PNG
    # 入参不被修改。
    assert "base64" in payload


def test_video_cover_base64_is_dropped_and_text_untouched(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", tmp_path)
    video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
    payload = {"message_type": "video", "base64": base64.b64encode(video).decode(), "image_base64": "QQ=="}
    result = externalize_media_payload(payload, tenant_id=1, channel_id=2, public_base_url="http://host")
    assert result["video_url"].endswith("/video.mp4")
    assert "image_base64" not in result and "base64" not in result

    text = {"message_type": "text", "content": "你好"}
    assert externalize_media_payload(text, tenant_id=1, channel_id=2, public_base_url="http://host") is text


def test_missing_public_base_url_keeps_payload_for_caller_to_reject(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(outbound_files, "OUTBOUND_FILE_ROOT", tmp_path)
    payload = {"message_type": "image", "base64": "QQ=="}
    assert externalize_media_payload(payload, tenant_id=1, channel_id=2, public_base_url="") is payload
    assert not list(Path(tmp_path).rglob("*.png"))


def test_redact_removes_nested_base64() -> None:
    value = {"output": {"replies": [{"type": "image", "base64": "A" * 1000, "image_base64": "B" * 10}]}, "n": 1}
    text = str(redact_base64_fields(value))
    assert "AAAA" not in text and "1000 字符" in text
    assert redact_base64_fields(value)["n"] == 1


def test_webridge_auto_outbound_maps_media_to_native_command(monkeypatch) -> None:
    """插件自动回复的视频、图片在微桥渠道必须变成 send_video/send_image 命令。

    之前这类载荷被入队成无 command 的普通出站任务,手机 poll 时被判定
    “普通出站链路暂只支持文本”而失败,视频永远发不出去。
    """

    from types import SimpleNamespace

    from app.services.wechat_webridge import service

    captured: list[dict] = []
    monkeypatch.setattr(service, "enqueue_job", lambda db, **kw: captured.append(kw) or SimpleNamespace(id=7))
    channel = SimpleNamespace(id=2, tenant_id=1)
    conversation = SimpleNamespace(id=4, external_id="wxid-bob")

    service.enqueue_message_command(_FakeDb(), channel, conversation, {"message_type": "video", "video_url": "http://h/outbound-files/2/x/video.mp4", "receiver_id": "wxid-bob"})
    service.enqueue_message_command(_FakeDb(), channel, conversation, {"message_type": "image", "image_url": "http://h/outbound-files/2/y/image.png", "receiver_id": "wxid-bob"})

    first, second = (item["payload"]["command"] for item in captured)
    assert first["type"] == "send_video_from_url" and first["params"]["source"].endswith("video.mp4")
    assert second["type"] == "send_image" and second["params"]["image_path"].endswith("image.png")
