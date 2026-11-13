from __future__ import annotations

import json
import os
import threading
from contextlib import ExitStack
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "music-plugin"
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATA_DIR", str(TEST_RUNTIME / "data"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_RUNTIME / 'app.db'}")
os.environ.setdefault("LOG_DIR", str(TEST_RUNTIME / "logs"))
os.environ.setdefault("AUTH_SEED_DEMO_USERS", "false")

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Plugin, PluginStateRecord
from app.models.saas import Tenant
from app.services.plugin_directory import _normalize_reply_item, get_discovered_plugin, load_directory_plugin
from app.services.plugin_manifest import parse_manifest, read_config_fields
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse, PluginHttpTimeoutError
from app.services.plugin_runtime.replies import PluginReplyError, music_reply, standard_replies
from plugins.music import service as qqmusic
from plugins.music.service import MusicApi, MusicConfig, MusicError, Song, request_guard


def song(mid="song001", **values):
    return {
        "mid": mid, "id": 1, "title": "晴天", "type": 0,
        "singer": [{"name": "歌手", "mid": "singer001"}],
        "album": {"mid": "album001"}, "file": {"media_mid": "media001"},
        "provider_trace": "do-not-retain", **values,
    }


class FakeService:
    def __init__(self, *, songs=None, routes=None, on_request=None):
        self.songs = [song()] if songs is None else songs
        self.routes = routes or {}
        self.on_request = on_request
        self.calls = []

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        self.calls.append((parsed.path, parse_qs(parsed.query), kwargs))
        if self.on_request:
            self.on_request(parsed.path)
        if parsed.path in self.routes:
            payload = self.routes[parsed.path]
            if isinstance(payload, Exception):
                raise payload
            if isinstance(payload, PluginHttpResponse):
                return payload
        else:
            if parsed.path.endswith("/search/search_by_type"):
                data = {"song": self.songs}
            elif parsed.path.endswith("/query_song"):
                data = {"tracks": self.songs}
            elif parsed.path.endswith("/get_cdn_dispatch"):
                data = {"sip": ["https://media.example.test/"]}
            elif parsed.path.endswith("/lyric"):
                data = {"lyric": "[00:01.00]第一行\n[00:02.50]第二行"}
            else:
                mid = parsed.path.split("/")[-2]
                data = {"data": [{
                    "mid": mid, "result": 0, "filename": "M500media001.mp3",
                    "purl": f"https://media.example.test/{mid}.mp3?vkey=play-token",
                    "vkey": "do-not-retain-vkey", "ekey": "do-not-retain-ekey",
                }]}
            payload = {"code": 0, "msg": "ok", "data": data}
        return PluginHttpResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode(), url)


@pytest.fixture(scope="module")
def music():
    loaded = load_directory_plugin("music", force=True)
    assert loaded and loaded.instance
    return loaded.instance._module


def request(content="点歌 晴天", *, config=None, context=None, **payload):
    return PluginExecutionRequest(
        action="execute", config={"api_base": "https://music.example.test", **(config or {})},
        payload={"content": content, **payload}, context=context,
    )


def test_manifest_exposes_valid_form_and_new_commands():
    path = PROJECT_ROOT / "plugins/music/config.toml"
    manifest = parse_manifest(path.read_text(encoding="utf-8"))
    fields, errors = read_config_fields(manifest)
    assert errors == []
    assert manifest["plugin"]["version"] == "3.0.0"
    by_key = {field.key: field for field in fields}
    assert set(manifest["runtime"]) <= set(by_key)
    assert by_key["api_key"].kind == "secret" and not by_key["api_key"].default
    assert {value for value, label in by_key["file_type"].options} == set(qqmusic.FILE_TYPES)
    discovered = get_discovered_plugin("music", force=True)
    assert discovered.is_valid
    assert {"搜歌", "选歌", "歌词"} <= set(discovered.matcher.commands)
    assert manifest["capability"]["timeout_ms"] == 30000


@pytest.mark.parametrize("file_type,code,extension", [
    ("mp3_128", 13, ".mp3"), ("mp3_320", 12, ".mp3"), ("flac", 7, ".flac"),
    ("ogg_192", 10, ".ogg"), ("ogg_320", 9, ".ogg"), ("aac_96", 15, ".m4a"), ("aac_192", 14, ".m4a"),
])
def test_web_file_type_integer_mapping(music, file_type, code, extension):
    fake = FakeService(routes={"/song/song001/url": {"code": 0, "data": {"data": [{
        "mid": "song001", "result": 0, "filename": f"audio{extension}", "purl": f"audio{extension}?vkey=temporary",
    }]}}})
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request(config={"file_type": file_type, "output_mode": "file"}))
    assert result.ok
    assert [call[0] for call in fake.calls] == ["/search/search_by_type", "/song/song001/url", "/song/get_cdn_dispatch"]
    assert fake.calls[0][1] == {"keyword": ["晴天"], "search_type": ["0"], "num": ["5"], "page": ["1"], "highlight": ["false"]}
    assert fake.calls[1][1] == {"file_type": [str(code)], "media_mid": ["media001"], "song_type": ["0"]}
    assert result.output["replies"][1]["file_name"].endswith(extension)
    assert result.output["replies"][1]["file_url"] == f"https://media.example.test/audio{extension}?vkey=temporary"
    assert all(call[2]["retry_count"] == 0 and call[2]["max_redirects"] == 0 for call in fake.calls)


@pytest.mark.parametrize("payload", [[], {"success": True, "data": []}, {"code": False, "data": {}},
    {"code": "0", "data": {}}, {"code": 0, "data": []}, {"code": 0, "data": {"song": {}}}])
def test_rejects_legacy_and_malformed_response(music, payload):
    fake = FakeService(routes={"/search/search_by_type": payload})
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request())
    assert not result.ok and result.output["provider_status"] == "incompatible"
    assert len(fake.calls) == 1


@pytest.mark.parametrize("status,headers,kind", [
    (401, {}, "auth"), (403, {}, "access_denied"), (429, {}, "limited"),
    (429, {"X-RateLimit-Limit": "100"}, "quota"), (422, {}, "incompatible"),
    (404, {}, "incompatible"), (504, {}, "timeout"), (500, {}, "upstream"),
])
def test_classifies_http_errors_without_retry(music, status, headers, kind):
    response = PluginHttpResponse(status, headers, b'{}', "https://music.example.test")
    fake = FakeService(routes={"/search/search_by_type": response, "/search/quick_search": response})
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request(config={"on_failure": "silent"}))
    assert not result.ok and result.output["provider_status"] == kind
    assert result.output["http_status"] == status and result.output["replies"] == []
    assert len(fake.calls) == (2 if kind == "limited" else 1)


def test_limited_search_uses_quick_search_and_matching_detail(music):
    fake = FakeService(routes={
        "/search/search_by_type": PluginHttpResponse(429, {}, b'{}', "https://music.example.test"),
        "/search/quick_search": {"code": 0, "data": {"song": {"itemlist": [
            {"mid": "song001", "name": "晴天", "singer": "歌手", "pic": "https://untrusted.test/cover"},
        ]}}},
        "/song/query_song": {"code": 0, "data": {"tracks": [song("other"), song()]}},
    })
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request())
    assert result.ok
    assert [call[0] for call in fake.calls] == ["/search/search_by_type", "/search/quick_search",
        "/song/query_song", "/song/song001/url", "/song/song001/lyric"]
    assert fake.calls[1][1] == {"keyword": ["晴天"]}
    assert fake.calls[2][1] == {"value": ["song001"]}
    card = result.output["replies"][0]
    assert card["song_album_url"].endswith("T002R300x300M000album001.jpg")
    assert "untrusted.test" not in json.dumps(result.output)
    fake.calls.clear()
    fake.routes["/song/query_song"]["data"]["tracks"] = [song("other")]
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request())
    assert not result.ok and result.output["failure_stage"] == "detail"
    assert len(fake.calls) == 3


@pytest.mark.parametrize("category", [None, [], {"itemlist": {}}])
def test_quick_search_requires_standard_category(category):
    fake = FakeService(routes={
        "/search/search_by_type": PluginHttpResponse(429, {}, b'{}', "https://music.example.test"),
        "/search/quick_search": {"code": 0, "data": {"song": category}},
    })
    with pytest.raises(MusicError) as error:
        MusicApi(MusicConfig(api_base="https://music.example.test"), fake).search("晴天")
    assert (error.value.kind, error.value.stage) == ("incompatible", "quick_search")
    assert len(fake.calls) == 2


@pytest.mark.parametrize("status,kind", [(429, "limited"), (500, "upstream")])
def test_quick_search_failure_stops_after_one_alternate(status, kind):
    fake = FakeService(routes={
        "/search/search_by_type": PluginHttpResponse(429, {}, b'{}', "https://music.example.test"),
        "/search/quick_search": PluginHttpResponse(status, {}, b'{}', "https://music.example.test"),
    })
    with pytest.raises(MusicError) as error:
        MusicApi(MusicConfig(api_base="https://music.example.test"), fake).search("晴天")
    assert (error.value.kind, error.value.stage) == (kind, "quick_search")
    assert len(fake.calls) == 2


def test_quick_search_filters_duplicates_and_bounds_results():
    fake = FakeService(routes={
        "/search/search_by_type": PluginHttpResponse(429, {}, b'{}', "https://music.example.test"),
        "/search/quick_search": {"code": 0, "data": {"song": {"itemlist": [None,
            {"mid": "bad/mid", "name": "无效"}, {"mid": "song001", "name": "晴天", "singer": "歌手"},
            {"mid": "song001", "name": "重复"}, {"mid": "song002", "name": "第二首"},
        ]}}},
    })
    songs = MusicApi(MusicConfig(api_base="https://music.example.test", result_limit=1), fake).search("晴天")
    assert songs == [Song("song001", "晴天", singer="歌手", needs_detail=True)]


def test_quick_search_cannot_extend_total_deadline():
    with patch.object(qqmusic.time, "monotonic", return_value=100) as timer:
        def finish_budget(path):
            timer.return_value = 124.95
        fake = FakeService(on_request=finish_budget, routes={
            "/search/search_by_type": PluginHttpResponse(429, {}, b'{}', "https://music.example.test"),
        })
        api = MusicApi(MusicConfig(api_base="https://music.example.test"), fake)
        with pytest.raises(MusicError) as error:
            api.search("晴天")
    assert (error.value.kind, error.value.stage) == ("timeout", "quick_search")
    assert len(fake.calls) == 1


@pytest.mark.parametrize("result_code,kind", [(104003, "unplayable"), (104004, "vkey_failed"), (104013, "device")])
def test_play_permission_failures_never_change_song(music, result_code, kind):
    fake = FakeService(songs=[song(), song("song002")], routes={"/song/song001/url": {
        "code": 0, "data": {"data": [{"mid": "song001", "result": result_code, "purl": ""}]},
    }})
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request())
    assert not result.ok and result.output["provider_status"] == kind
    assert [call[0] for call in fake.calls] == ["/search/search_by_type", "/song/song001/url"]


@pytest.mark.parametrize("item,kind", [
    ({"mid": "other", "result": 0, "purl": "https://media.example.test/a.mp3"}, "incompatible"),
    ({"mid": "song001", "result": True, "purl": "https://media.example.test/a.mp3"}, "incompatible"),
    ({"mid": "song001", "result": 0, "purl": ""}, "unplayable"),
    ({"mid": "song001", "result": 0, "filename": "a.mflac", "purl": "a.mflac"}, "incompatible"),
    ({"mid": "song001", "result": 0, "filename": "opaque", "purl": "a.mflac"}, "incompatible"),
])
def test_requires_matching_mid_and_real_audio(item, kind):
    api = MusicApi(MusicConfig(api_base="https://music.example.test", include_lyrics=False), FakeService(routes={
        "/song/song001/url": {"code": 0, "data": {"data": [item]}},
    }))
    with pytest.raises(MusicError) as error:
        api.resolve(Song("song001", "晴天"))
    assert error.value.kind == kind


@pytest.mark.parametrize("purl", ["//evil.example/a.mp3", "file:///song.mp3", "https://u:p@media.example/a.mp3",
    "https://media.example:99999/a.mp3", "https://media.example/\nsong.mp3", "\\evil\\song.mp3", "https://[invalid/a.mp3"])
def test_rejects_unsafe_play_urls(purl):
    api = MusicApi(MusicConfig(api_base="https://music.example.test"), FakeService())
    with pytest.raises(MusicError, match="incompatible"):
        api._play_url(purl)


def test_relative_cdn_ignores_invalid_hosts_and_preserves_query():
    fake = FakeService(routes={"/song/get_cdn_dispatch": {"code": 0, "data": {"sip": [
        "file:///tmp", "https://u:p@bad.example/", "https://cdn.example.test/base/",
    ]}}})
    api = MusicApi(MusicConfig(api_base="https://music.example.test"), fake)
    assert api._play_url("audio.mp3?vkey=x") == "https://cdn.example.test/base/audio.mp3?vkey=x"


def test_detail_matches_mid_and_preserves_song_type(music):
    record = song()
    record.pop("file")
    fake = FakeService(songs=[record], routes={"/song/query_song": {
        "code": 0, "data": {"tracks": [song("other"), song(file={"media_mid": "detailmedia"})]},
    }})
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request())
    assert result.ok
    assert fake.calls[1][1] == {"value": ["song001"], "song_type": ["0"]}
    assert fake.calls[2][1]["media_mid"] == ["detailmedia"]
    fake.routes["/song/query_song"]["data"]["tracks"] = [song("other")]
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request())
    assert not result.ok and result.output["failure_stage"] == "detail"


def test_music_reply_and_optional_lyrics(music):
    fake = FakeService()
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request(config={"app_id": "wx904fb3ecf62c7dea", "app_name": "音乐来源"}))
    card = result.output["replies"][0]
    assert card["type"] == "music" and card["song_lyric"].startswith("[00:01.00]")
    assert card["song_album_url"].endswith("T002R300x300M000album001.jpg")
    assert (card["app_id"], card["app_name"]) == ("wx904fb3ecf62c7dea", "汽水音乐")
    assert fake.calls[-1][1] == {"song_type": ["0"]}
    assert "do-not-retain" not in json.dumps(result.output)
    fake.routes["/song/song001/lyric"] = PluginHttpTimeoutError("safe-timeout")
    with patch.object(music, "PluginHttpClient", return_value=fake):
        assert music.execute_action(request()).output["replies"][0]["song_lyric"] == ""


def test_builtin_card_catalog_is_unique_and_shared_with_form():
    sources = qqmusic.card_sources()
    assert len(sources) == len(dict(sources)) == 307
    assert sources[0] == ("wx904fb3ecf62c7dea", "汽水音乐")
    fields, errors = read_config_fields(parse_manifest((PROJECT_ROOT / "plugins/music/config.toml").read_text(encoding="utf-8")))
    assert not errors
    field = next(item for item in fields if item.key == "app_id")
    assert field.kind == "select" and field.options == sources
    assert field.default == qqmusic.DEFAULT_CARD_APP_ID
    assert "app_name" not in {item.key for item in fields}
    assert sum(name == "摇一摇" for _key, name in sources) == 2


@pytest.mark.parametrize("config, expected", [
    ({}, ("wx904fb3ecf62c7dea", "汽水音乐")),
    ({"app_id": "", "app_name": ""}, ("wx904fb3ecf62c7dea", "汽水音乐")),
    ({"app_id": "wx8dd6ecd81906fd84", "app_name": "QQ音乐"}, ("wx8dd6ecd81906fd84", "网易云音乐")),
    ({"app_id": "WX79F2C4418704B4F8"}, ("wx79f2c4418704b4f8", "酷狗音乐")),
    ({"app_id": "wx485a97c844086dc9"}, ("wx485a97c844086dc9", "摇一摇")),
    ({"app_id": "wx5aa333606550dfd5", "app_name": "其他名称"}, ("wx5aa333606550dfd5", "QQ音乐")),
])
def test_music_card_source_is_resolved_without_changing_music_provider(music, config, expected):
    fake = FakeService()
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request(config=config))
    card = result.output["replies"][0]
    assert result.ok and (card["app_id"], card["app_name"]) == expected
    assert all(kwargs["max_redirects"] == 0 for _path, _query, kwargs in fake.calls)
    assert card["url"].startswith("https://y.qq.com/")


@pytest.mark.parametrize("source", ["摇一摇", "酷我音乐", "酷", "wx123", "wx000000000000000g", "wx0000000000000001"])
def test_incomplete_or_ambiguous_card_source_does_not_call_provider(music, source):
    with patch.object(music, "PluginHttpClient") as client:
        result = music.execute_action(request(config={"app_id": source}))
    assert not result.ok and result.output["provider_status"] == "configuration_error"
    client.assert_not_called()


def test_source_form_only_saves_catalog_ids():
    from app.services.plugin_schemas import build_plugin_config_view, merge_schema_config

    view = build_plugin_config_view("music", None)
    assert "app_name" not in {field["name"] for field in view["fields"]}
    for value in ("wx0000000000000001", "汽水音乐", "search text"):
        merged = merge_schema_config("music", {"schema__app_id": value})
        assert json.loads(merged.config_json)["app_id"] == qqmusic.DEFAULT_CARD_APP_ID
    merged = merge_schema_config("music", {"schema__app_id": "wx8dd6ecd81906fd84"})
    assert json.loads(merged.config_json)["app_id"] == "wx8dd6ecd81906fd84"


def test_lyrics_are_bounded_and_cover_falls_back_to_singer(music):
    fake = FakeService(songs=[song(album={}, singer=[{"name": "歌手", "pmid": "singerfallback"}])], routes={
        "/song/song001/lyric": {"code": 0, "data": {"lyric": "词" * 6000}},
    })
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request())
    card = result.output["replies"][0]
    assert len(card["song_lyric"]) == 5000
    assert card["song_album_url"].endswith("T001R300x300M000singerfallback.jpg")


@pytest.mark.parametrize("config", [{"include_lyrics": False}, {"output_mode": "text"}, {"output_mode": "file"}])
def test_skips_optional_lyrics_when_not_used(music, config):
    fake = FakeService()
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request(config=config))
    assert result.ok and len(fake.calls) == 2


def test_lyric_command_decrypt_contract_and_empty_lyrics(music):
    fake = FakeService()
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request("歌词 晴天", config={"include_lyrics": False}))
    assert result.output["replies"][0]["content"] == "晴天 — 歌手\n第一行\n第二行"
    fake.routes["/song/song001/lyric"] = {"code": 0, "data": {"lyric": ""}}
    with patch.object(music, "PluginHttpClient", return_value=fake):
        assert music.execute_action(request("歌词 晴天")).message == "这首歌暂无歌词。"


@pytest.mark.parametrize("content,index", [("点歌 晴天 -n 2", 2), ("/点歌 -n 2 晴天", 2), ("音乐 晴天", 1)])
def test_command_index_and_aliases(music, content, index):
    fake = FakeService(songs=[song(), song("song002", title="另一个版本")])
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request(content))
    assert result.ok and result.output["song_id"] == f"song00{index}"


@pytest.mark.parametrize("content", ["点歌 晴天 -n 0", "点歌 晴天 -n x", "点歌 晴天 -n", "选歌 -1", "选歌 1 2"])
def test_invalid_index_before_network(music, content):
    with patch.object(music, "PluginHttpClient") as client:
        result = music.execute_action(request(content))
    assert not result.ok and result.output["provider_status"] == "invalid_index"
    client.assert_not_called()


def test_platform_stripped_args_support_custom_prefix_and_empty_keyword(music):
    with patch.object(music, "PluginHttpClient") as client:
        result = music.execute_action(request("!点歌", command_name="点歌", command_args=""))
    assert result.output["provider_status"] == "keyword_required"
    client.assert_not_called()


@pytest.mark.parametrize("config", [
    {"file_type": "exhigh"}, {"output_mode": "unknown"}, {"on_failure": "unknown"},
    {"api_base": "https://music.example.test?"}, {"api_base": "https://music.example.test#"},
    {"api_base": "https://user:pass@music.example.test"}, {"api_key": "proxy\r\nextra"},
])
def test_invalid_configuration_does_not_call_service(music, config):
    fake = FakeService()
    with patch.object(music, "PluginHttpClient", return_value=fake):
        result = music.execute_action(request(config=config))
    assert not result.ok and result.output["provider_status"] == "configuration_error"
    assert fake.calls == []


def test_search_without_trusted_context_does_not_offer_retained_selection(music):
    with patch.object(music, "PluginHttpClient", return_value=FakeService()):
        result = music.execute_action(request("搜歌 晴天"))
    assert result.ok and "点歌 歌名 -n" in result.message
    assert music.execute_action(request("选歌 1")).output["provider_status"] == "trusted_context_required"


@pytest.fixture()
def music_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'music.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    contexts = []
    with platform_session(factory) as db:
        plugin = Plugin(code="music", display_name="音乐点播", plugin_type="command")
        db.add(plugin)
        for index in range(2):
            tenant = Tenant(code=f"music-{index}", name=f"音乐租户{index}", status="active")
            db.add(tenant)
            db.flush()
            bot = Bot(tenant_id=tenant.id, name=f"音乐机器人{index}", is_enabled=True, status="running")
            db.add(bot)
            db.flush()
            channel = Channel(tenant_id=tenant.id, bot_id=bot.id, name="微桥", channel_type="wechat_webridge",
                              is_enabled=True, runtime_info_json=json.dumps({"webridge": {"account_id": "a1", "device_id": "d1", "session_id": "s1"}}))
            binding = BotPlugin(tenant_id=tenant.id, bot_id=bot.id, plugin_id=plugin.id, status="enabled")
            db.add_all([channel, binding])
            db.flush()
            conversation = Conversation(tenant_id=tenant.id, bot_id=bot.id, channel_id=channel.id,
                channel_type=channel.channel_type, external_id="same-room", chat_type="group")
            db.add(conversation)
            db.flush()
            contexts.append(PluginExecutionContext(tenant_id=tenant.id, binding_id=binding.id, bot_id=bot.id,
                channel_id=channel.id, conversation_id=conversation.id, sender_id="same-sender", channel_type=channel.channel_type))
        db.commit()
    yield factory, contexts
    engine.dispose()


def test_selection_retains_only_metadata_and_clears_failed_search(music, music_db):
    factory, (context, _) = music_db
    fake = FakeService(songs=[song(), song("song002", title="另一版本")])
    with tenant_scope(context.tenant_id), tenant_session(factory) as db, patch.object(music, "PluginHttpClient", return_value=fake):
        first = music.execute_message_action(db, request("搜歌 晴天", context=context))
        assert first.ok and "选歌 2" in first.message
        row = db.scalar(select(PluginStateRecord))
        state = json.loads(row.payload_json)
        assert len(state["songs"]) == 2
        assert not any(value in row.payload_json for value in ("do-not-retain", "vkey", "purl", "https://"))
        selected = music.execute_message_action(db, request("选歌 2", context=context))
        assert selected.ok and selected.output["song_id"] == "song002"
        assert len([call for call in fake.calls if call[0] == "/search/search_by_type"]) == 1
        assert music.execute_message_action(db, request("选歌 3", context=context)).output["provider_status"] == "invalid_index"
        fake.routes["/search/search_by_type"] = PluginHttpTimeoutError("test")
        assert not music.execute_message_action(db, request("搜歌 新歌", context=context)).ok
        assert db.scalar(select(PluginStateRecord)) is None
        assert music.execute_message_action(db, request("选歌 1", context=context)).output["provider_status"] == "selection_expired"


def test_selection_user_conversation_binding_channel_and_tenant_isolation(music, music_db):
    factory, (context, other) = music_db
    with tenant_scope(context.tenant_id), tenant_session(factory) as db, patch.object(music, "PluginHttpClient", return_value=FakeService()):
        assert music.execute_message_action(db, request("搜歌 晴天", context=context)).ok
        channel = db.get(Channel, context.channel_id)
        conversation = Conversation(tenant_id=context.tenant_id, bot_id=context.bot_id, channel_id=channel.id,
            channel_type=channel.channel_type, external_id="other-room", chat_type="group")
        binding = BotPlugin(tenant_id=context.tenant_id, bot_id=context.bot_id,
            plugin_id=db.get(BotPlugin, context.binding_id).plugin_id, status="enabled")
        other_channel = Channel(tenant_id=context.tenant_id, bot_id=context.bot_id, name="另一渠道", channel_type="wechat_webridge", is_enabled=True)
        db.add_all([conversation, binding, other_channel])
        db.flush()
        channel_conversation = Conversation(tenant_id=context.tenant_id, bot_id=context.bot_id, channel_id=other_channel.id,
            channel_type=other_channel.channel_type, external_id="same-room", chat_type="group")
        db.add(channel_conversation)
        db.flush()
        for isolated in (replace(context, sender_id="other-sender"), replace(context, conversation_id=conversation.id),
            replace(context, binding_id=binding.id), replace(context, channel_id=other_channel.id, conversation_id=channel_conversation.id)):
            assert music.execute_message_action(db, request("选歌 1", context=isolated)).output["provider_status"] == "selection_expired"
        db.commit()
    with tenant_scope(other.tenant_id), tenant_session(factory) as db:
        assert music.execute_message_action(db, request("选歌 1", context=other)).output["provider_status"] == "selection_expired"
        invalid = replace(context, tenant_id=other.tenant_id)
        assert music.execute_message_action(db, request("选歌 1", context=invalid)).output["provider_status"] == "trusted_context_required"


@pytest.mark.parametrize("change", ["expiry", "account", "session", "device", "service", "quality", "proxy"])
def test_selection_invalidated_by_expiry_account_or_configuration(music, music_db, change):
    factory, (context, _) = music_db
    with tenant_scope(context.tenant_id), tenant_session(factory) as db, patch.object(music, "PluginHttpClient", return_value=FakeService()):
        music.execute_message_action(db, request("搜歌 晴天", context=context))
        config = {}
        if change == "expiry":
            db.scalar(select(PluginStateRecord)).expires_at = now_shanghai() - timedelta(seconds=1)
            db.flush()
        elif change in {"account", "session", "device"}:
            channel = db.get(Channel, context.channel_id)
            info = json.loads(channel.runtime_info_json)
            info["webridge"][f"{change}_id"] = "changed"
            channel.runtime_info_json = json.dumps(info)
            db.flush()
        else:
            config = {"service": {"api_base": "https://new.example.test"}, "quality": {"file_type": "flac"}, "proxy": {"api_key": "new-proxy-key"}}[change]
        assert music.execute_message_action(db, request("选歌 1", config=config, context=context)).output["provider_status"] == "selection_expired"


@pytest.mark.parametrize("change", ["account", "disabled", "deleted", "binding"])
@pytest.mark.parametrize("fail", [False, True])
def test_context_changed_during_request_discards_success_and_failure(music, music_db, change, fail):
    factory, (context, _) = music_db
    with tenant_scope(context.tenant_id), tenant_session(factory) as db:
        def mutate(path):
            if not path.endswith("/url"):
                return
            channel = db.get(Channel, context.channel_id)
            if change == "account":
                channel.runtime_info_json = '{"webridge":{"account_id":"switched"}}'
            elif change == "disabled":
                channel.is_enabled = False
            elif change == "deleted":
                channel.deleted_at = now_shanghai()
            else:
                db.get(BotPlugin, context.binding_id).status = "disabled"
            db.flush()
        fake = FakeService(on_request=mutate, routes={"/song/song001/url": PluginHttpTimeoutError("test")} if fail else {})
        with patch.object(music, "PluginHttpClient", return_value=fake):
            result = music.execute_message_action(db, request(context=context))
        assert not result.ok and result.output == {"provider_status": "context_changed", "replies": []}


def test_request_guard_releases_errors_and_limits_per_binding():
    with pytest.raises(RuntimeError):
        with request_guard(100, 100, "failed"):
            raise RuntimeError("test")
    with request_guard(100, 100, "failed"):
        with pytest.raises(MusicError, match="pending"):
            with request_guard(100, 100, "failed"):
                pass
    with ExitStack() as stack:
        for index in range(16):
            stack.enter_context(request_guard(100, 100, str(index)))
        with pytest.raises(MusicError, match="busy"):
            with request_guard(100, 100, "extra"):
                pass
        with request_guard(100, 101, "extra"):
            pass
    assert not qqmusic._pending


def test_request_guard_global_cap():
    with ExitStack() as stack:
        for binding in range(4):
            for index in range(16):
                stack.enter_context(request_guard(101, binding, str(index)))
        with pytest.raises(MusicError, match="busy"):
            with request_guard(101, 5, "extra"):
                pass
    assert not qqmusic._pending


def test_total_deadline_limits_remaining_request_budget():
    fake = FakeService()
    with patch.object(qqmusic.time, "monotonic", return_value=100) as timer:
        api = MusicApi(MusicConfig(api_base="https://music.example.test", timeout_seconds=25, request_timeout_seconds=8), fake)
        timer.return_value = 120
        api.search("晴天")
        assert fake.calls[0][2]["total_timeout_seconds"] == 5
        timer.return_value = 125
        with pytest.raises(MusicError, match="timeout"):
            api.search("晴天")
    assert len(fake.calls) == 1


def test_real_http_client_against_loopback_v090_contract(music):
    fake = FakeService()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            response = fake.request("GET", f"http://localhost{self.path}")
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = music.execute_action(request(config={"api_base": f"http://127.0.0.1:{server.server_port}"}))
        assert result.ok and result.output["replies"][0]["type"] == "music"
        assert len(fake.calls) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_standard_music_reply_validation_and_channel_fallbacks():
    card = music_reply(title="晴天", desc="歌手", url="https://y.qq.com/song001", data_url="https://media.example.test/a.mp3",
        song_lyric="歌词", app_id="test-app", app_name="音乐来源")
    assert standard_replies([card]) == [card]
    native = _normalize_reply_item(card, channel_type="wechat_webridge")
    assert native["message_type"] == "music" and native["song_lyric"] == "歌词"
    for channel in get_discovered_plugin("music", force=True).capability.channel_types:
        if channel == "wechat_webridge":
            continue
        fallback = _normalize_reply_item(card, channel_type=channel)
        assert fallback["message_type"] == "text"
        assert "晴天\n歌手" in fallback["content"] and card["data_url"] in fallback["content"]
    assert _normalize_reply_item({"type": "file", "file_name": "song.mp3", "file_url": card["data_url"]}, channel_type="telegram")["message_type"] == "text"
    for values in ({"data_url": "file:///a.mp3"}, {"song_album_url": "https://u:p@host/a"}, {"song_lyric": "x" * 5001}, {"title": ""}):
        with pytest.raises(PluginReplyError):
            music_reply(**{key: value for key, value in {**card, **values}.items() if key != "type"})
