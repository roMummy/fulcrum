"""QQMusicApi v0.9.0 Web contract; no SDK credentials or channel transport here."""

from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlencode, urljoin, urlsplit

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpTimeoutError
from app.services.plugin_manifest import parse_manifest


# v0.9.0 web/src/modules/song.py publishes integer file_type values, not SDK enum names.
# Only ordinary, directly playable formats are exposed. Encrypted/preview files are excluded.
FILE_TYPES = {
    "mp3_128": (13, ".mp3"),
    "mp3_320": (12, ".mp3"),
    "flac": (7, ".flac"),
    "ogg_192": (10, ".ogg"),
    "ogg_320": (9, ".ogg"),
    "aac_96": (15, ".m4a"),
    "aac_192": (14, ".m4a"),
}
MAX_KEYWORD_LENGTH = 80
MAX_LYRIC_LENGTH = 5000
DEFAULT_CARD_APP_ID = "wx904fb3ecf62c7dea"


class MusicError(RuntimeError):
    def __init__(self, kind: str, stage: str, http_status: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.stage = stage
        self.http_status = http_status


@dataclass(frozen=True)
class MusicConfig:
    api_base: str
    api_key: str = ""
    file_type: str = "mp3_128"
    timeout_seconds: int = 25
    request_timeout_seconds: int = 8
    result_limit: int = 5
    selection_timeout_seconds: int = 180
    include_lyrics: bool = True
    output_mode: str = "auto"
    on_failure: str = "message"
    app_id: str = DEFAULT_CARD_APP_ID
    app_name: str = "汽水音乐"


@dataclass(frozen=True)
class Song:
    mid: str
    title: str
    singer: str = ""
    album_mid: str = ""
    singer_mid: str = ""
    media_mid: str = ""
    song_type: int | None = None
    needs_detail: bool = False

    @property
    def page_url(self) -> str:
        return f"https://y.qq.com/n/ryqq/songDetail/{self.mid}"

    @property
    def cover_url(self) -> str:
        # Same album -> singer fallback as the v0.9.0 Song.cover_url() helper.
        if self.album_mid:
            return f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{self.album_mid}.jpg"
        if self.singer_mid:
            return f"https://y.gtimg.cn/music/photo_new/T001R300x300M000{self.singer_mid}.jpg"
        return ""


@dataclass(frozen=True)
class PlayableSong:
    song: Song
    play_url: str
    extension: str
    lyric: str = ""


def inline_text(value: Any, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


@lru_cache(maxsize=1)
def card_sources() -> tuple[tuple[str, str], ...]:
    """The manifest is the single catalog used by both the form and runtime."""
    manifest = parse_manifest(Path(__file__).with_name("config.toml").read_text(encoding="utf-8"))
    field = next(item for item in manifest["config"]["fields"] if item["key"] == "app_id")
    return tuple((item["value"], item["label"]) for item in field["options"])


def resolve_card_source(app_id: Any) -> tuple[str, str]:
    value = inline_text(app_id) or DEFAULT_CARD_APP_ID
    catalog = dict(card_sources())
    if value.lower() in catalog:
        return value.lower(), catalog[value.lower()]
    raise MusicError("configuration_error", "config")


def identifier(value: Any) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else ""


def http_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return ""
    if re.search(r"[\s\x00-\x1f\x7f\\]", value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        parsed.port  # Validate malformed/out-of-range ports, too.
    except ValueError:
        return ""
    return value


def song_from_payload(value: Any) -> Song | None:
    if not isinstance(value, Mapping):
        return None
    mid = identifier(value.get("mid"))
    title = inline_text(value.get("title") or value.get("name"))
    if not mid or not title:
        return None
    singers = value.get("singer")
    singers = [item for item in singers if isinstance(item, Mapping)] if isinstance(singers, list) else []
    album = value.get("album") if isinstance(value.get("album"), Mapping) else {}
    file = value.get("file") if isinstance(value.get("file"), Mapping) else {}
    singer_mid = next((identifier(item.get("mid") or item.get("pmid")) for item in singers
                       if identifier(item.get("mid") or item.get("pmid"))), "")
    song_type = value.get("type")
    return Song(
        mid=mid,
        title=title,
        singer=" / ".join(filter(None, (inline_text(item.get("name"), 80) for item in singers)))[:300],
        album_mid=identifier(album.get("mid") or album.get("pmid")),
        singer_mid=singer_mid,
        media_mid=identifier(file.get("media_mid")),
        song_type=song_type if type(song_type) is int and song_type >= 0 else None,
        needs_detail=not bool(file),
    )


class MusicApi:
    def __init__(self, config: MusicConfig, client: PluginHttpClient | None = None):
        self.config = config
        self.client = client or PluginHttpClient()
        self.deadline = time.monotonic() + config.timeout_seconds
        self.base = http_url(config.api_base.rstrip("/"))
        if not self.base or "?" in self.base or "#" in self.base:
            raise MusicError("configuration_error", "config")
        if config.file_type not in FILE_TYPES:
            raise MusicError("configuration_error", "config")
        if re.search(r"[\x00-\x1f\x7f]", config.api_key):
            raise MusicError("configuration_error", "config")

    def get(self, path: str, stage: str, parameters: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0.1:
            raise MusicError("timeout", stage)
        timeout = min(self.config.request_timeout_seconds, remaining)
        headers = {"Accept": "application/json", "User-Agent": "Fulcrum-Music/3.0"}
        # Optional reverse-proxy authorization; QQ account credentials stay in the upstream service.
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        query = urlencode(parameters or {})
        url = f"{self.base}{path}" + (f"?{query}" if query else "")
        try:
            response = self.client.request(
                "GET", url, headers=headers,
                connect_timeout_seconds=min(5, timeout), total_timeout_seconds=timeout,
                max_response_bytes=2 * 1024 * 1024, max_redirects=0, retry_count=0,
            )
        except PluginHttpTimeoutError as exc:
            raise MusicError("timeout", stage) from exc
        except PluginHttpError as exc:
            raise MusicError("upstream", stage) from exc
        status = response.status_code
        if status == 401:
            raise MusicError("auth", stage, status)
        if status == 403:
            raise MusicError("access_denied", stage, status)
        if status == 429:
            response_headers = {str(key).lower(): value for key, value in response.headers.items()}
            kind = "quota" if "x-ratelimit-limit" in response_headers else "limited"
            raise MusicError(kind, stage, status)
        if status in {404, 405, 422}:
            raise MusicError("incompatible", stage, status)
        if status == 504:
            raise MusicError("timeout", stage, status)
        if status != 200:
            raise MusicError("upstream", stage, status)
        if time.monotonic() >= self.deadline:
            raise MusicError("timeout", stage)
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise MusicError("incompatible", stage, status) from exc
        if not isinstance(payload, Mapping) or type(payload.get("code")) is not int:
            raise MusicError("incompatible", stage, status)
        if payload["code"] != 0:
            raise MusicError("upstream", stage, status)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MusicError("incompatible", stage, status)
        return data

    def search(self, keyword: str) -> list[Song]:
        try:
            data = self.get("/search/search_by_type", "search", {
                "keyword": keyword, "search_type": 0, "num": self.config.result_limit,
                "page": 1, "highlight": "false",
            })
        except MusicError as exc:
            if exc.kind != "limited":
                raise
            # One alternate official search, only for upstream throttling, never Web quota exhaustion.
            data = self.get("/search/quick_search", "quick_search", {"keyword": keyword})
            category = data.get("song")
            values = category.get("itemlist") if isinstance(category, Mapping) else None
            if not isinstance(values, list):
                raise MusicError("incompatible", "quick_search")
            songs: dict[str, Song] = {}
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                mid, title = identifier(value.get("mid")), inline_text(value.get("name"))
                if mid and title:
                    songs.setdefault(mid, Song(mid=mid, title=title, singer=inline_text(value.get("singer"), 300),
                                               needs_detail=True))
                if len(songs) >= self.config.result_limit:
                    break
            return list(songs.values())
        values = data.get("song")
        if not isinstance(values, list):
            raise MusicError("incompatible", "search")
        songs: dict[str, Song] = {}
        for value in values:
            song = song_from_payload(value)
            if song is not None:
                songs.setdefault(song.mid, song)
            if len(songs) >= self.config.result_limit:
                break
        return list(songs.values())

    def lyric(self, song: Song) -> str:
        params = {"song_type": song.song_type} if song.song_type is not None else {}
        data = self.get(f"/song/{song.mid}/lyric", "lyric", params)
        value = data.get("lyric")
        if not isinstance(value, str):
            raise MusicError("incompatible", "lyric")
        return value.strip()[:MAX_LYRIC_LENGTH]

    def resolve(self, song: Song) -> PlayableSong:
        if song.needs_detail:
            detail_params: dict[str, Any] = {"value": song.mid}
            if song.song_type is not None:
                detail_params["song_type"] = song.song_type
            data = self.get("/song/query_song", "detail", detail_params)
            tracks = data.get("tracks")
            if not isinstance(tracks, list):
                raise MusicError("incompatible", "detail")
            detail = next((item for item in tracks if isinstance(item, Mapping) and item.get("mid") == song.mid), None)
            matched = song_from_payload(detail)
            if matched is None:
                raise MusicError("incompatible", "detail")
            song = replace(matched, needs_detail=False)
        file_code, extension = FILE_TYPES[self.config.file_type]
        params: dict[str, Any] = {"file_type": file_code}
        if song.media_mid:
            params["media_mid"] = song.media_mid
        if song.song_type is not None:
            params["song_type"] = song.song_type
        data = self.get(f"/song/{song.mid}/url", "play", params)
        values = data.get("data")
        if not isinstance(values, list):
            raise MusicError("incompatible", "play")
        item = next((value for value in values if isinstance(value, Mapping) and value.get("mid") == song.mid), None)
        if item is None or type(item.get("result")) is not int:
            raise MusicError("incompatible", "play")
        result = item["result"]
        if result != 0:
            kind = {104003: "unplayable", 104004: "vkey_failed", 104013: "device"}.get(result, "unplayable")
            raise MusicError(kind, "play")
        purl = item.get("purl")
        if not isinstance(purl, str) or not purl:
            raise MusicError("unplayable", "play")
        play_url = self._play_url(purl)
        filename = item.get("filename")
        candidates = [filename if isinstance(filename, str) else "", urlsplit(purl).path]
        extensions = [match[1].lower() for value in candidates
                      if (match := re.search(r"(\.[A-Za-z0-9]+)$", value))]
        if any(value not in {entry[1] for entry in FILE_TYPES.values()} for value in extensions):
            raise MusicError("incompatible", "play")
        if extensions:
            extension = extensions[0]
        lyric = ""
        if self.config.include_lyrics and self.config.output_mode == "auto" and self.deadline - time.monotonic() > 0.5:
            try:
                lyric = self.lyric(song)
            except MusicError:
                # Lyrics are optional; a failure must not discard an authorized playable result.
                pass
        return PlayableSong(song=song, play_url=play_url, extension=extension, lyric=lyric)

    def _play_url(self, purl: str) -> str:
        if len(purl) > 4096 or re.search(r"[\s\x00-\x1f\x7f\\]", purl):
            raise MusicError("incompatible", "play")
        try:
            parsed = urlsplit(purl)
        except ValueError as exc:
            raise MusicError("incompatible", "play") from exc
        if parsed.scheme:
            result = http_url(purl)
            if not result:
                raise MusicError("incompatible", "play")
            return result
        if parsed.netloc or purl.startswith("//"):
            raise MusicError("incompatible", "play")
        cdn = self.get("/song/get_cdn_dispatch", "cdn")
        if not isinstance(cdn.get("sip"), list):
            raise MusicError("incompatible", "cdn")
        for value in cdn["sip"]:
            base = http_url(value)
            if not base:
                continue
            result = http_url(urljoin(base, purl))
            if result and urlsplit(result).netloc == urlsplit(base).netloc and urlsplit(result).scheme == urlsplit(base).scheme:
                return result
        raise MusicError("incompatible", "cdn")


_pending_lock = threading.Lock()
_pending: set[tuple[int, int, str]] = set()


@contextmanager
def request_guard(tenant_id: int, binding_id: int, identity: str) -> Iterator[None]:
    """Bound work per binding/worker; upstream v0.9.0 supplies cross-worker concurrency controls."""
    key = (tenant_id, binding_id, identity)
    with _pending_lock:
        if key in _pending:
            raise MusicError("pending", "request")
        if len(_pending) >= 64 or sum(item[:2] == key[:2] for item in _pending) >= 16:
            raise MusicError("busy", "request")
        _pending.add(key)
    try:
        yield
    finally:
        with _pending_lock:
            _pending.discard(key)
