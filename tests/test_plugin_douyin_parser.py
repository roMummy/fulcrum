"""抖音解析插件的定向测试:作品归一化、DTK 与自有解析顺序、发送策略。

全部使用 PluginHttpClient 替身,不访问真实网络。
"""

from __future__ import annotations

import base64
import io
import json
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from app.services.plugin_runtime.contracts import PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpError, PluginHttpResponse
from plugins.douyin_parser import parsers as parse
from plugins.douyin_parser import plugin, service


def _jpeg(width: int = 100, height: int = 50, fmt: str = "JPEG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format=fmt)
    return buffer.getvalue()


def _resp(body: bytes | dict, *, status: int = 200, content_type: str = "application/json", url: str = "", headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    merged = {"content-type": content_type, **(headers or {})}
    return PluginHttpResponse(status_code=status, headers=merged, body=raw, url=url)


class FakeClient:
    """按 URL 子串路由的 HTTP 替身,记录每次调用。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _answer(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value(url) if callable(value) else value
        raise PluginHttpError("no route " + url)

    def request(self, method, url, **kwargs):
        return self._answer(method, url, kwargs)

    def post_json(self, url, payload, **kwargs):
        kwargs["payload"] = payload
        return self._answer("POST", url, kwargs)


VIDEO_AWEME = {
    "aweme_id": "7400000000000000001",
    "desc": "测试视频",
    "author": {"nickname": "作者甲"},
    "video": {
        "duration": 12000,
        "play_addr": {"uri": "v0200abc", "url_list": ["https://x.test/playwm/?a=1"]},
        "cover": {"url_list": ["https://c.test/a.webp", "https://c.test/a.jpeg"]},
    },
}
NOTE_AWEME = {
    "aweme_id": "7400000000000000002",
    "desc": "图文",
    "author": {"nickname": "作者乙"},
    "images": [
        {"display_image": {"url_list": [f"https://i.test/{n}.webp", f"https://i.test/{n}.jpeg"]}} for n in range(5)
    ],
}


def test_parse_video_picks_stable_play_url_cover_and_duration() -> None:
    media = parse.parse_aweme(VIDEO_AWEME)
    assert media["type"] == "video"
    assert media["url"] == "https://aweme.snssdk.com/aweme/v1/play/?video_id=v0200abc&ratio=720p&line=0"
    assert media["cover"] == "https://c.test/a.jpeg"  # JPEG 优先于 WEBP
    assert media["duration_seconds"] == 12.0
    assert media["author"] == "作者甲"


def test_parse_note_prefers_jpeg_and_detects_live_pages() -> None:
    media = parse.parse_aweme(NOTE_AWEME)
    assert media["type"] == "image" and len(media["note_pages"]) == 5
    assert media["note_pages"][0]["image_candidates"][0] == "https://i.test/0.jpeg"
    live = {
        "display_image": {"url_list": ["https://i.test/l.jpeg"]},
        "video": {"play_addr": {"url_list": ["https://v3-web.douyinvod.com/live.mp4"]}},
    }
    pages = parse.build_note_pages([live])
    assert pages[0]["media_type"] == "video"
    assert pages[0]["video_candidates"] == ["https://v3-web.douyinvod.com/live.mp4"]


def test_duration_requires_explicit_unit_and_rejects_bad_values() -> None:
    assert parse.normalize_duration_seconds(20000, milliseconds=True) == 20.0
    assert parse.normalize_duration_seconds(True) is None
    assert parse.normalize_duration_seconds(0) is None
    assert parse.normalize_duration_seconds("nan") is None


def test_map_dtk_uses_raw_then_falls_back_to_normalized_fields() -> None:
    from_raw = parse.map_dtk_data({"raw": {"aweme_detail": VIDEO_AWEME}})
    assert from_raw["type"] == "video" and from_raw["author"] == "作者甲"
    manual = parse.map_dtk_data(
        {
            "content_kind": "video",
            "caption": "手工映射",
            "author": {"nickname": "作者丙"},
            "duration_ms": 8000,
            "video": {"play_url": "https://m.test/v.mp4", "cover_url": "https://m.test/c.jpg"},
        }
    )
    assert manual["url"] == "https://m.test/v.mp4" and manual["duration_seconds"] == 8.0
    album = parse.map_dtk_data({"content_kind": "image_album", "images": [{"url": "https://a.test/1.jpg"}, "https://a.test/2.jpg"]})
    assert album["type"] == "image" and len(album["note_pages"]) == 2
    assert parse.map_dtk_data({"content_kind": "video"}) is None


def test_share_html_router_data_and_legacy() -> None:
    router = {"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": [VIDEO_AWEME]}}}}
    page = f"<script>window._ROUTER_DATA = {json.dumps(router)}</script>"
    assert parse.parse_share_html(page)["type"] == "video"
    legacy = '"desc":"旧版","nickname":"老作者","play_addr":{"uri":"u","url_list":["https://aweme.snssdk.com/play/?x=1"]}'
    parsed = parse.parse_share_html(legacy)
    assert parsed["url"].startswith("https://aweme.snssdk.com") and parsed["title"] == "旧版"


def _dtk_ok(data):
    return _resp({"success": True, "data": data})


def test_dtk_success_skips_local_parsing() -> None:
    client = FakeClient({"/api/v1/parse": _dtk_ok({"raw": VIDEO_AWEME})})
    media = service.parse_media(
        client, "https://v.douyin.com/x/", dtk_api_url="https://dtk.test", dtk_api_key="k", dtk_wait_seconds=15,
        deadline=time.monotonic() + 30,
    )
    assert media["type"] == "video"
    method, url, kwargs = client.calls[0]
    assert url == "https://dtk.test/api/v1/parse?wait=15"
    assert kwargs["payload"] == {"url": "https://v.douyin.com/x/", "include_raw": True}
    assert kwargs["headers"]["X-API-Key"] == "k"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "dtk_response",
    [
        _resp({"success": True, "data": {"task_id": "t"}}, status=202),
        _resp({"success": False, "error": {"code": "BUSY"}}, status=500),
        _resp(b"<html>", content_type="text/html"),
        PluginHttpError("down"),
    ],
)
def test_dtk_failure_falls_back_to_local_detail(dtk_response) -> None:
    client = FakeClient(
        {
            "/api/v1/parse": dtk_response,
            "v.douyin.com": _resp(b"<html></html>", content_type="text/html", url="https://www.douyin.com/video/7400000000000000001"),
            "ttwid.bytedance.com": _resp({}, headers={"set-cookie": "ttwid=abc123; Path=/"}),
            "aweme/detail": _resp({"status_code": 0, "aweme_detail": VIDEO_AWEME}),
        }
    )
    service._ttwid_cache = ("", 0.0)
    media = service.parse_media(
        client, "https://v.douyin.com/x/", dtk_api_url="https://dtk.test", dtk_api_key="k", dtk_wait_seconds=15,
        deadline=time.monotonic() + 30,
    )
    assert media["type"] == "video"
    detail_call = next(call for call in client.calls if "aweme/detail" in call[1])
    assert "a_bogus=" in detail_call[1] and detail_call[2]["headers"]["Cookie"] == "ttwid=abc123"


def test_local_falls_back_to_slides_api_and_total_failure_raises() -> None:
    client = FakeClient(
        {
            "v.douyin.com": _resp(b"<html>shell</html>", content_type="text/html", url="https://www.iesdouyin.com/share/slides/7400000000000000002/"),
            "slidesinfo": _resp({"status_code": 0, "aweme_details": [NOTE_AWEME]}),
        }
    )
    media = service.parse_media(
        client, "https://v.douyin.com/x/", dtk_api_url="", dtk_api_key="", dtk_wait_seconds=15, deadline=time.monotonic() + 30
    )
    assert media["type"] == "image"
    assert not any("/api/v1/parse" in call[1] for call in client.calls)  # 未配置 DTK 不请求

    broken = FakeClient({"v.douyin.com": _resp(b"<html></html>", content_type="text/html", url="https://www.douyin.com/user/1")})
    with pytest.raises(service.SourceError):
        service.parse_media(
            broken, "https://v.douyin.com/x/", dtk_api_url="", dtk_api_key="", dtk_wait_seconds=15, deadline=time.monotonic() + 30
        )


def _media_client(video_ok: bool = True):
    return FakeClient(
        {
            ".mp4": _resp(b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4") if video_ok else _resp(b"x", status=403),
            "/play/": _resp(b"\x00\x00\x00\x18ftypmp42", content_type="video/mp4") if video_ok else _resp(b"x", status=403),
            "/c.jpg": _resp(_jpeg(), content_type="image/jpeg"),
        }
    )


def _short_video(duration):
    return {"type": "video", "url": "https://m.test/v.mp4", "title": "标题", "author": "作者", "cover": "https://m.test/c.jpg", "duration_seconds": duration}


def test_short_video_is_sent_directly_with_cover() -> None:
    replies = service.build_replies(_media_client(), _short_video(20.0), download_mode="card", timeout_seconds=10)
    assert [r["type"] for r in replies] == ["video"] and replies[0]["play_length"] == 20


@pytest.mark.parametrize("duration", [20.001, None, 90])
def test_long_or_unknown_duration_falls_back_to_link_card(duration) -> None:
    client = _media_client()
    replies = service.build_replies(client, _short_video(duration), download_mode="card", timeout_seconds=10)
    assert replies[0]["type"] == "link" and replies[0]["title"] == "作者" and replies[0]["description"] == "标题"
    assert not client.calls  # 不下载视频


def test_short_video_download_failure_falls_back_to_link_card() -> None:
    replies = service.build_replies(_media_client(video_ok=False), _short_video(5), download_mode="card", timeout_seconds=10)
    assert replies[0]["type"] == "link"


def test_file_mode_sends_remote_file_without_downloading() -> None:
    client = _media_client()
    replies = service.build_replies(client, _short_video(300), download_mode="file", timeout_seconds=10)
    assert replies == [{"type": "file", "file_name": "作者 - 标题.mp4", "file_url": "https://m.test/v.mp4"}]
    assert not client.calls


def _note(count: int):
    pages = [
        {"media_type": "image", "image_candidates": [f"https://i.test/{n}.jpg"], "video_candidates": [], "cover_candidates": []}
        for n in range(count)
    ]
    return {"type": "image", "title": "文案", "author": "作者", "note_pages": pages}


def _image_client():
    return FakeClient({"https://i.test/": _resp(_jpeg(600, 800, "PNG"), content_type="image/png")})


def test_note_with_few_images_sends_text_then_each_image() -> None:
    replies = service.build_replies(_image_client(), _note(2), download_mode="card", timeout_seconds=10)
    assert [r["type"] for r in replies] == ["text", "image", "image"]
    assert "作者:作者" in replies[0]["content"] and "文案:文案" in replies[0]["content"]
    decoded = Image.open(io.BytesIO(base64.b64decode(replies[1]["base64"])))
    assert decoded.format == "JPEG"  # PNG 统一转成 JPEG


def test_note_with_many_images_is_merged_vertically_and_paged() -> None:
    replies = service.build_replies(_image_client(), _note(6), download_mode="card", timeout_seconds=10)
    images = [r for r in replies if r["type"] == "image"]
    assert len(images) == 1
    merged = Image.open(io.BytesIO(base64.b64decode(images[0]["base64"])))
    assert merged.width == 1242 and merged.height == 6 * int(800 * 1242 / 600)
    # 超过单页高度上限时分页
    many = service._merge_vertical([_jpeg(1242, 12000)] * 5)
    assert len(many) > 1


def test_live_note_sends_video_with_cover_and_degrades_to_image() -> None:
    page = {
        "media_type": "video", "image_candidates": ["https://i.test/a.jpg"], "video_candidates": ["https://v.test/a.mp4"],
        "cover_candidates": ["https://i.test/a.jpg"],
    }
    media = {"type": "image", "title": "", "author": "", "note_pages": [page, {**page, "video_candidates": ["https://v.test/dead.mp4"]}]}
    client = FakeClient(
        {
            "https://v.test/a.mp4": _resp(b"video", content_type="video/mp4"),
            "https://v.test/dead.mp4": _resp(b"", status=404),
            "https://i.test/": _resp(_jpeg(), content_type="image/jpeg"),
        }
    )
    replies = service.build_replies(client, media, download_mode="card", timeout_seconds=10)
    assert [r["type"] for r in replies] == ["video", "image"]


def test_all_images_failing_raises_instead_of_sending_only_text() -> None:
    client = FakeClient({"https://i.test/": _resp(b"<html>", content_type="text/html")})
    with pytest.raises(service.PluginReplyError):
        service.build_replies(client, _note(2), download_mode="card", timeout_seconds=10)


def test_extract_url_rejects_spoofed_hosts_and_trims_punctuation() -> None:
    assert plugin._extract_douyin_url("看 https://v.douyin.com/abc123/,好看") == "https://v.douyin.com/abc123/"
    assert plugin._extract_douyin_url("https://douyin.com.attacker.test/video/1") == ""
    assert plugin._extract_douyin_url("https://u:p@v.douyin.com/x") == ""
    assert plugin._extract_douyin_url("file:///tmp/a.mp4") == ""
    ctx = SimpleNamespace(chat_type="group", message_type="text", content="https://www.douyin.com/video/1234567890123")
    assert plugin.matches_event(ctx)["matched"] is True


def test_execute_action_end_to_end_hides_secrets_and_maps_errors(monkeypatch) -> None:
    client = FakeClient({"/api/v1/parse": _dtk_ok({"raw": VIDEO_AWEME}), "aweme.snssdk.com": _resp(b"v", content_type="video/mp4"), "c.test": _resp(_jpeg(), content_type="image/jpeg")})
    monkeypatch.setattr(plugin, "PluginHttpClient", lambda: client)
    request = PluginExecutionRequest(
        action="execute",
        config={"enabled": True, "dtk_api_url": "https://dtk.test", "dtk_api_key": "SECRET-KEY"},
        payload={"content": "https://v.douyin.com/x/"},
    )
    result = plugin.execute_action(request)
    assert result.ok and result.output["replies"][0]["type"] == "video"
    assert "SECRET-KEY" not in json.dumps(result.output, ensure_ascii=False)

    failing = FakeClient({})
    monkeypatch.setattr(plugin, "PluginHttpClient", lambda: failing)
    bad = plugin.execute_action(request)
    assert not bad.ok and bad.output["provider_status"] == "parse_failed"
    assert "SECRET-KEY" not in json.dumps(bad.output, ensure_ascii=False)
