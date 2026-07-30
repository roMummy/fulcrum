from __future__ import annotations

import base64
import json
import random
import time
import urllib.parse
from html import escape
from io import BytesIO
from typing import Any
from urllib import request as urlrequest

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = "multi-bot-admin/1.0"
DEFAULT_CLIP_DURATION_SECONDS = 10
DEFAULT_HINT_TIMEOUT_SECONDS = 30
DEFAULT_SINGERS = (
    "周杰伦",
    "林俊杰",
    "薛之谦",
    "陈奕迅",
    "邓紫棋",
    "李荣浩",
    "张学友",
    "刘德华",
    "Beyond",
    "孙燕姿",
)
MUSIC_XML_TEMPLATE = """
<appmsg appid="wx45116b30f23e0cc4" sdkver="0">
    <title>{song_name}</title>
    <des>{singer_name}-click to play</des>
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


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = request.config
    payload = dict(request.payload)
    music_api_url = str(config.get("music_api_url") or "http://192.168.2.240:5000").strip()
    song_api_url = str(config.get("song_api_url") or "").strip()
    detail_level = str(config.get("detail_level") or "exhigh").strip() or "exhigh"
    timeout = _coerce_int(config.get("default_timeout"), 10)
    clip_duration = max(3, _coerce_int(config.get("clip_duration"), DEFAULT_CLIP_DURATION_SECONDS))
    hint_timeout = max(5, _coerce_int(config.get("hint_timeout"), DEFAULT_HINT_TIMEOUT_SECONDS))
    popular_singers = _normalize_singers(config.get("popular_singers"))
    sessions = _normalize_sessions(config.get("game_sessions"))
    conversation_key = _resolve_conversation_key(payload)
    current_game = _resolve_current_game(payload, sessions, conversation_key)

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="guess_song runtime is ready",
            output={
                "music_api_url": music_api_url,
                "song_api_url": song_api_url,
                "detail_level": detail_level,
                "default_timeout": timeout,
                "clip_duration": clip_duration,
                "hint_timeout": hint_timeout,
                "popular_singers": list(popular_singers),
                "active_session_count": len(sessions),
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")

    operation = str(payload.get("operation") or "").strip().lower()
    if not operation:
        operation = _infer_operation(payload, current_game)

    try:
        if operation in {"answer", "guess"}:
            return _answer_game(
                payload=payload,
                current_game=current_game,
                sessions=sessions,
                conversation_key=conversation_key,
                hint_timeout=hint_timeout,
            )
        if operation in {"next", "skip"}:
            return _start_game(
                singer_name=str(payload.get("singer") or payload.get("artist") or "").strip() or None,
                previous_game=current_game,
                sessions=sessions,
                conversation_key=conversation_key,
                music_api_url=music_api_url,
                song_api_url=song_api_url,
                detail_level=detail_level,
                timeout=timeout,
                clip_duration=clip_duration,
                popular_singers=popular_singers,
                provider_status="next_song",
            )
        return _start_game(
            singer_name=_resolve_singer_name(payload),
            previous_game=current_game,
            sessions=sessions,
            conversation_key=conversation_key,
            music_api_url=music_api_url,
            song_api_url=song_api_url,
            detail_level=detail_level,
            timeout=timeout,
            clip_duration=clip_duration,
            popular_singers=popular_singers,
            provider_status="started",
        )
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"guess_song failed: {exc}")


def _start_game(
    *,
    singer_name: str | None,
    previous_game: dict[str, Any],
    sessions: dict[str, dict[str, Any]],
    conversation_key: str,
    music_api_url: str,
    song_api_url: str,
    detail_level: str,
    timeout: int,
    clip_duration: int,
    popular_singers: tuple[str, ...],
    provider_status: str,
) -> PluginExecutionResult:
    song = _search_random_song(
        singer_name=singer_name,
        music_api_url=music_api_url,
        song_api_url=song_api_url,
        detail_level=detail_level,
        timeout=timeout,
        popular_singers=popular_singers,
    )
    if not song.get("play_url"):
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=f"no song found for {singer_name or 'random'}",
            output={
                "provider_status": "empty",
                "singer_name": singer_name or "",
                "reply_text": f"没有找到 {singer_name or '随机'} 歌曲，请稍后再试。",
                "replies": _text_reply(f"没有找到 {singer_name or '随机'} 歌曲，请稍后再试。"),
            },
        )

    current_game = {
        "status": "playing",
        "song_name": song.get("song_name", ""),
        "singer_name": song.get("singer_name", ""),
        "play_url": song.get("play_url", ""),
        "data_url": song.get("data_url", ""),
        "pic_url": song.get("pic_url", ""),
        "clip_duration": clip_duration,
        "started_at": int(time.time()),
        "hint_given": False,
    }
    updated_sessions = dict(sessions)
    updated_sessions[conversation_key] = current_game

    replies: list[dict[str, Any]] = []
    output: dict[str, Any] = {
        "provider_status": provider_status,
        "singer_name": current_game["singer_name"],
        "song_name_mask": _mask_song_name(str(current_game["song_name"])),
        "clip_preview": {
            "source_url": current_game["play_url"],
            "clip_duration_seconds": clip_duration,
            "transport": "voice",
        },
        "current_game": current_game,
        "config_patch": {"game_sessions": updated_sessions},
    }

    if previous_game and provider_status == "next_song":
        previous_song_name = str(previous_game.get("song_name") or "").strip()
        previous_singer_name = str(previous_game.get("singer_name") or "").strip()
        previous_music_xml = _build_music_xml(previous_game)
        previous_answer_text = f"已跳过上一首：{previous_singer_name} - {previous_song_name}".strip(" -")
        output["previous_answer"] = {
            "song_name": previous_song_name,
            "singer_name": previous_singer_name,
        }
        output["previous_music_xml"] = previous_music_xml
        replies.extend(_text_reply(previous_answer_text))
        replies.extend(_xml_reply(previous_music_xml))

    voice_reply = _build_clip_reply(current_game, timeout=timeout)
    if voice_reply:
        replies.append(voice_reply)
        output["voice_available"] = True
    else:
        output["voice_available"] = False

    reply_text = _build_round_prompt(
        singer_name=str(current_game["singer_name"]),
        song_name_mask=_mask_song_name(str(current_game["song_name"])),
        clip_duration=clip_duration,
    )
    output["reply_text"] = reply_text
    replies.extend(_text_reply(reply_text))
    output["replies"] = replies
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="guess song round generated" if provider_status == "started" else "previous song skipped and next round generated",
        output=output,
    )


def _answer_game(
    *,
    payload: dict[str, Any],
    current_game: dict[str, Any],
    sessions: dict[str, dict[str, Any]],
    conversation_key: str,
    hint_timeout: int,
) -> PluginExecutionResult:
    if not current_game:
        text = "当前没有进行中的猜歌游戏，先发送“猜歌”开始吧。"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="no active game",
            output={"provider_status": "empty", "reply_text": text, "replies": _text_reply(text)},
        )

    answer = str(
        payload.get("answer")
        or payload.get("guess")
        or payload.get("command_args")
        or payload.get("stripped_content")
        or payload.get("content")
        or ""
    ).strip()
    if not answer:
        text = "请直接回复歌名，例如：猜歌 稻香"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="missing answer",
            output={"provider_status": "missing_answer", "reply_text": text, "replies": _text_reply(text)},
        )

    song_name = str(current_game.get("song_name") or "").strip()
    singer_name = str(current_game.get("singer_name") or "").strip()
    if answer.lower() and answer.lower() in song_name.lower():
        updated_sessions = dict(sessions)
        updated_sessions.pop(conversation_key, None)
        music_xml = _build_music_xml(current_game)
        reply_text = f"答对了，答案是：{singer_name} - {song_name}"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="answer is correct",
            output={
                "provider_status": "answered",
                "correct": True,
                "answer": answer,
                "song_name": song_name,
                "singer_name": singer_name,
                "music_xml": music_xml,
                "reply_text": reply_text,
                "config_patch": {"game_sessions": updated_sessions},
                "replies": _text_reply(reply_text) + _xml_reply(music_xml),
            },
        )

    started_at = _coerce_int(current_game.get("started_at"), 0)
    elapsed_seconds = _coerce_int(payload.get("elapsed_seconds"), 0)
    if elapsed_seconds <= 0 and started_at > 0:
        elapsed_seconds = max(0, int(time.time()) - started_at)
    hint_available = elapsed_seconds >= hint_timeout
    hint_given = bool(current_game.get("hint_given"))
    updated_game = dict(current_game)
    updated_sessions = dict(sessions)
    hint = ""
    if hint_available:
        hint = _mask_song_name(song_name)
        if not hint_given:
            updated_game["hint_given"] = True
            updated_sessions[conversation_key] = updated_game
    reply_text = _build_incorrect_answer_text(
        singer_name=singer_name,
        masked_name=_mask_song_name(song_name),
        hint=hint,
    )
    output: dict[str, Any] = {
        "provider_status": "answered",
        "correct": False,
        "answer": answer,
        "song_name_mask": _mask_song_name(song_name),
        "singer_name": singer_name,
        "hint": hint,
        "hint_available": hint_available,
        "current_game": updated_game,
        "reply_text": reply_text,
        "replies": _text_reply(reply_text),
    }
    if hint and updated_sessions.get(conversation_key) != current_game:
        output["config_patch"] = {"game_sessions": updated_sessions}
    return PluginExecutionResult(ok=True, status="completed", message="answer is incorrect", output=output)


def _search_random_song(
    *,
    singer_name: str | None,
    music_api_url: str,
    song_api_url: str,
    detail_level: str,
    timeout: int,
    popular_singers: tuple[str, ...],
) -> dict[str, str]:
    keyword = singer_name or random.choice(popular_singers)
    search_url = _build_search_url(music_api_url, keyword)
    search_data = _get_json(search_url, timeout)
    if not bool(search_data.get("success")) or int(search_data.get("status") or 0) != 200:
        raise ValueError(str(search_data.get("message") or "search provider returned unsuccessful response"))
    songs = search_data.get("data") or []
    if not isinstance(songs, list) or not songs:
        return {}
    candidates = [item for item in songs if isinstance(item, dict)]
    if not candidates:
        return {}
    selected = random.choice(candidates)
    song_id = str(selected.get("id") or "").strip()
    if not song_id:
        return {}

    detail_url = _build_detail_url(music_api_url, song_api_url, song_id, detail_level)
    detail_data = _get_json(detail_url, timeout)
    if not bool(detail_data.get("success")) or int(detail_data.get("status") or 0) != 200:
        raise ValueError(str(detail_data.get("message") or "song detail provider returned unsuccessful response"))
    detail = detail_data.get("data") or {}
    if not isinstance(detail, dict):
        detail = {}

    play_url = _resolve_redirect(str(detail.get("url") or ""))
    pic_url = _resolve_redirect(str(detail.get("pic") or selected.get("picUrl") or ""))
    return {
        "song_id": song_id,
        "song_name": str(detail.get("name") or selected.get("name") or "").strip(),
        "singer_name": str(detail.get("ar_name") or selected.get("artist_string") or selected.get("artists") or keyword).strip(),
        "play_url": play_url,
        "data_url": play_url,
        "pic_url": pic_url,
    }


def _build_search_url(music_api_url: str, keyword: str) -> str:
    if "{keywords}" in music_api_url:
        return music_api_url.format(keywords=urllib.parse.quote(keyword))
    base = music_api_url.split("?")[0].rstrip("/")
    return f"{base}/Search?keywords={urllib.parse.quote(keyword)}&limit=10"


def _build_detail_url(music_api_url: str, song_api_url: str, song_id: str, detail_level: str) -> str:
    if song_api_url:
        return song_api_url.format(url=song_id, level=detail_level, type="json")
    base = music_api_url.split("?")[0].rstrip("/")
    return f"{base}/Song_V1?url={song_id}&level={urllib.parse.quote(detail_level)}&type=json"


def _build_clip_reply(song: dict[str, Any], *, timeout: int) -> dict[str, Any] | None:
    play_url = str(song.get("play_url") or "").strip()
    if not play_url:
        return None
    clip_duration = max(3, _coerce_int(song.get("clip_duration"), DEFAULT_CLIP_DURATION_SECONDS))
    try:
        clip_payload = _download_and_clip_audio(play_url, clip_duration, timeout=timeout)
    except Exception:
        return None
    if not clip_payload:
        return None
    return {
        "type": "voice",
        "base64": clip_payload["base64"],
        "voice_time": clip_payload["duration_ms"],
        "voice_type": 4,
    }


def _download_and_clip_audio(play_url: str, clip_duration: int, *, timeout: int) -> dict[str, Any] | None:
    audio_bytes = _download_binary(play_url, timeout=timeout)
    if not audio_bytes:
        return None
    audio_format = _guess_audio_format(play_url)
    clipped = _encode_clip_to_silk(audio_bytes, clip_duration=clip_duration, audio_format=audio_format)
    if not clipped:
        return None
    return {
        "base64": base64.b64encode(clipped["bytes"]).decode("ascii"),
        "duration_ms": int(clipped["duration_ms"]),
    }


def _encode_clip_to_silk(audio_bytes: bytes, *, clip_duration: int, audio_format: str | None) -> dict[str, Any]:
    try:
        from pydub import AudioSegment
    except ImportError as exc:
        raise ValueError("pydub is required for guess_song voice rendering") from exc
    try:
        import pysilk
    except ImportError as exc:
        raise ValueError("pysilk is required for guess_song voice rendering") from exc

    stream = BytesIO(audio_bytes)
    audio = AudioSegment.from_file(stream, format=audio_format) if audio_format else AudioSegment.from_file(stream)
    clip = audio[: clip_duration * 1000]
    clip = clip.set_channels(1)
    sample_rate = _get_closest_frame_rate(clip.frame_rate)
    clip = clip.set_frame_rate(sample_rate).set_sample_width(2)
    encoded_bytes = pysilk.encode(clip.raw_data, sample_rate=sample_rate)
    return {"bytes": encoded_bytes, "duration_ms": len(clip)}


def _build_music_xml(song: dict[str, Any]) -> str:
    return MUSIC_XML_TEMPLATE.format(
        song_name=escape(str(song.get("song_name") or "")),
        singer_name=escape(str(song.get("singer_name") or "")),
        data_url=escape(str(song.get("data_url") or "")),
        play_url=escape(str(song.get("play_url") or "")),
        singer_pic=escape(str(song.get("pic_url") or "")),
    )


def _mask_song_name(song_name: str) -> str:
    normalized = song_name.strip()
    if not normalized:
        return ""
    if len(normalized) == 1:
        return normalized
    return normalized[0] + "*" * (len(normalized) - 1)


def _resolve_singer_name(payload: dict[str, Any]) -> str | None:
    singer = str(payload.get("singer") or payload.get("artist") or "").strip()
    if singer:
        return singer
    query = str(
        payload.get("command_args")
        or payload.get("stripped_query")
        or payload.get("query")
        or payload.get("keyword")
        or ""
    ).strip()
    if query in {"下一首", "跳过"}:
        return None
    return query or None


def _infer_operation(payload: dict[str, Any], current_game: dict[str, Any]) -> str:
    raw = str(
        payload.get("command_args")
        or payload.get("stripped_content")
        or payload.get("stripped_query")
        or payload.get("content")
        or payload.get("query")
        or payload.get("keyword")
        or ""
    ).strip().lower()
    if raw in {"next", "skip", "下一首", "跳过"}:
        return "next"
    if current_game and raw:
        return "answer"
    return "start"


def _resolve_current_game(payload: dict[str, Any], sessions: dict[str, dict[str, Any]], conversation_key: str) -> dict[str, Any]:
    payload_game = payload.get("current_game")
    if isinstance(payload_game, dict):
        return dict(payload_game)
    return dict(sessions.get(conversation_key) or {})


def _resolve_conversation_key(payload: dict[str, Any]) -> str:
    return str(
        payload.get("conversation_external_id")
        or payload.get("conversation_id")
        or payload.get("room_id")
        or payload.get("roomid")
        or payload.get("sender_id")
        or payload.get("user")
        or "global"
    ).strip()


def _normalize_sessions(raw_value: object) -> dict[str, dict[str, Any]]:
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw_value, dict):
        return {}
    sessions: dict[str, dict[str, Any]] = {}
    for key, value in raw_value.items():
        if not isinstance(value, dict):
            continue
        normalized_key = str(key).strip()
        if normalized_key:
            sessions[normalized_key] = dict(value)
    return sessions


def _normalize_singers(raw_value: object) -> tuple[str, ...]:
    if isinstance(raw_value, list):
        singers = tuple(str(item).strip() for item in raw_value if str(item).strip())
        return singers or DEFAULT_SINGERS
    if isinstance(raw_value, str):
        singers = tuple(item.strip() for item in raw_value.split(",") if item.strip())
        return singers or DEFAULT_SINGERS
    return DEFAULT_SINGERS


def _resolve_redirect(url: str) -> str:
    if not url:
        return ""
    req = urlrequest.Request(url, method="HEAD", headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            return getattr(response, "url", url) or url
    except Exception:
        return url


def _download_binary(url: str, *, timeout: int) -> bytes:
    req = urlrequest.Request(url, method="GET", headers={"User-Agent": DEFAULT_USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        return response.read()


def _get_json(url: str, timeout: int) -> dict[str, Any]:
    req = urlrequest.Request(url, method="GET", headers={"accept": "application/json", "User-Agent": DEFAULT_USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")


def _guess_audio_format(url: str) -> str | None:
    path = urllib.parse.urlparse(url).path
    if "." not in path:
        return None
    extension = path.rsplit(".", 1)[-1].strip().lower()
    if extension in {"mp3", "wav", "ogg", "flac", "m4a", "aac"}:
        return extension
    return None


def _get_closest_frame_rate(frame_rate: int) -> int:
    supported = [8000, 12000, 16000, 24000]
    return min(supported, key=lambda candidate: abs(candidate - frame_rate))


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text_reply(content: str) -> list[dict[str, str]]:
    normalized = str(content).strip()
    return [{"type": "text", "content": normalized}] if normalized else []


def _xml_reply(xml: str) -> list[dict[str, str]]:
    normalized = str(xml).strip()
    return [{"type": "xml", "xml": normalized}] if normalized else []


def _build_round_prompt(*, singer_name: str, song_name_mask: str, clip_duration: int) -> str:
    lines = ["猜歌开始", f"歌手：{singer_name}", f"歌名：{song_name_mask}", f"片段时长：{clip_duration}s"]
    lines.append("请直接回复“猜歌 歌名”来作答，或发送“猜歌 下一首”跳过。")
    return "\n".join(line for line in lines if line.strip())


def _build_incorrect_answer_text(*, singer_name: str, masked_name: str, hint: str) -> str:
    lines = ["答案不对，再试试", f"歌手：{singer_name}", f"歌名：{masked_name}"]
    if hint:
        lines.append(f"提示：{hint}")
    return "\n".join(line for line in lines if line.strip())
