from __future__ import annotations

import json
from html import escape
import urllib.parse
from urllib import request as urlrequest

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = "multi-bot-admin/1.0"
DEFAULT_COMMANDS = ("点歌", "音乐", "音乐点播", "点播音乐", "音乐点歌")
MUSIC_XML_TEMPLATE = """
<appmsg appid="wx45116b30f23e0cc4" sdkver="0">
    <title>{song_name}</title>
    <des>{singer_name}-点击三角直接播放</des>
    <action>view</action>
    <type>3</type>
    <showtype>0</showtype>
    <content />
    <url>{data_url}</url>
    <dataurl>{play_url}</dataurl>
    <songalbumurl>{singer_pic}</songalbumurl>
</appmsg>
<scene>0</scene>
<appinfo>
    <version>1</version>
    <appname/>
</appinfo>
<commenturl />
""".strip()


def _xml_reply(xml: str) -> list[dict[str, str]]:
    normalized = str(xml).strip()
    return [{"type": "xml", "xml": normalized}] if normalized else []


def _text_reply(content: str) -> list[dict[str, str]]:
    normalized = str(content).strip()
    return [{"type": "text", "content": normalized}] if normalized else []


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    music_api_url = str(request.config.get("music_api_url") or "http://192.168.2.240:5000").strip()
    song_api_url = str(request.config.get("song_api_url") or "").strip()
    timeout = int(request.config.get("default_timeout", 10) or 10)
    detail_level = str(request.config.get("detail_level") or "exhigh").strip() or "exhigh"

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="音乐点播插件已就绪，可执行搜歌请求。",
            output={
                "music_api_url": music_api_url,
                "song_api_url": song_api_url,
                "default_timeout": timeout,
                "detail_level": detail_level,
                "commands": list(DEFAULT_COMMANDS),
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")

    keyword = _resolve_keyword(request.payload)
    if not keyword:
        reply_text = "命令格式错误，请使用：点歌 歌曲名"
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="音乐点播执行缺少 keyword/query/content。",
            output={"reply_text": reply_text, "replies": _text_reply(reply_text)},
        )

    try:
        song = _search_song(
            keyword=keyword,
            music_api_url=music_api_url,
            song_api_url=song_api_url,
            timeout=timeout,
            detail_level=detail_level,
        )
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"音乐点播请求失败: {exc}")

    if not song.get("play_url"):
        reply_text = f"未找到可播放的歌曲：{keyword}"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="未找到可播放的歌曲结果。",
            output={
                "provider_status": "empty",
                "keyword": keyword,
                "reply_text": reply_text,
                "replies": _text_reply(reply_text),
            },
        )

    music_xml = _build_music_xml(song)
    reply_text = f"已命中歌曲：{song.get('singer_name') or '-'} - {song.get('song_name') or keyword}"
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"音乐点播完成，已命中 {song.get('song_name') or keyword}。",
        output={
            "provider_status": "found",
            "keyword": keyword,
            **song,
            "reply_text": reply_text,
            "music_xml": music_xml,
            "replies": _xml_reply(music_xml),
        },
    )


def _resolve_keyword(payload: dict[str, object]) -> str:
    explicit = str(payload.get("keyword") or payload.get("command_args") or "").strip()
    if explicit:
        return explicit
    for key in ("stripped_query", "stripped_content", "query", "content"):
        normalized = _strip_music_command(str(payload.get(key) or "").strip())
        if normalized:
            return normalized
    return ""


def _strip_music_command(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = normalized.replace("\n", " ")
    tokens = [item for item in normalized.split(" ") if item]
    if not tokens:
        return ""
    first = tokens[0].lstrip("/").strip().lower()
    if first in {"点歌", "音乐", "音乐点播", "点播音乐", "音乐点歌"}:
        return " ".join(tokens[1:]).strip()
    return normalized


def _search_song(*, keyword: str, music_api_url: str, song_api_url: str, timeout: int, detail_level: str) -> dict[str, str]:
    if "{keywords}" in music_api_url:
        search_url = music_api_url.format(keywords=urllib.parse.quote(keyword))
    else:
        base = music_api_url.split("?")[0].rstrip("/")
        search_url = f"{base}/Search?keyword={urllib.parse.quote(keyword)}&limit=10"
    search_data = _get_json(search_url, timeout)
    if not bool(search_data.get("success")) or int(search_data.get("status") or 0) != 200:
        raise ValueError(str(search_data.get("message") or "search provider returned unsuccessful response"))
    songs = search_data.get("data") or []
    if not isinstance(songs, list) or not songs:
        return {}
    first_song = songs[0] if isinstance(songs[0], dict) else {}
    song_id = str(first_song.get("id") or "").strip()
    if not song_id:
        return {}
    if song_api_url:
        detail_url = song_api_url.format(url=song_id, level=detail_level, type="json")
    else:
        base = music_api_url.split("?")[0].rstrip("/")
        detail_url = f"{base}/Song_V1?url={song_id}&level={urllib.parse.quote(detail_level)}&type=json"
    detail_data = _get_json(detail_url, timeout)
    if not bool(detail_data.get("success")) or int(detail_data.get("status") or 0) != 200:
        raise ValueError(str(detail_data.get("message") or "song detail provider returned unsuccessful response"))
    song_info = detail_data.get("data") or {}
    if not isinstance(song_info, dict):
        song_info = {}
    song_url = str(song_info.get("url") or "").strip()
    return {
        "song_id": song_id,
        "song_name": str(song_info.get("name") or first_song.get("name") or "").strip(),
        "singer_name": str(song_info.get("ar_name") or first_song.get("artist_string") or first_song.get("artists") or "").strip(),
        "play_url": _resolve_redirect(song_url),
        "data_url": _resolve_redirect(song_url),
        "pic_url": _resolve_redirect(str(song_info.get("pic") or first_song.get("picUrl") or "").strip()),
    }


def _build_music_xml(song: dict[str, str]) -> str:
    return MUSIC_XML_TEMPLATE.format(
        song_name=escape(str(song.get("song_name") or "")),
        singer_name=escape(str(song.get("singer_name") or "")),
        data_url=escape(str(song.get("data_url") or "")),
        play_url=escape(str(song.get("play_url") or "")),
        singer_pic=escape(str(song.get("pic_url") or "")),
    )


def _resolve_redirect(url: str) -> str:
    if not url:
        return ""
    req = urlrequest.Request(url, method="HEAD", headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return getattr(response, "url", url) or url
    except Exception:
        return url


def _get_json(url: str, timeout: int) -> dict:
    req = urlrequest.Request(url, method="GET", headers={"User-Agent": DEFAULT_USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")
