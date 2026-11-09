from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import file_reply, standard_replies, text_reply


COMMANDS = ("点歌", "音乐", "音乐点播", "点播音乐", "音乐点歌")
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; MultiBotSaaS-Music/2.0)"


@dataclass(frozen=True)
class MusicConfig:
    """保存租户自备音乐聚合服务配置。"""

    api_base: str
    api_key: str
    quality: str
    timeout_seconds: int


@dataclass(frozen=True)
class SongResult:
    """插件可返回和持久化的最小歌曲结果，不包含上游完整响应。"""

    song_id: str
    song_name: str
    artist_name: str
    play_url: str
    cover_url: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """通过租户音乐服务搜索首条可播放结果，并返回跨渠道标准回复。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="音乐点播插件已就绪。",
            output={
                "api_base_configured": bool(config.api_base),
                "api_key_configured": bool(config.api_key),
                "quality": config.quality,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not config.api_base:
        return _error_result("请先配置当前租户的音乐服务地址。", provider_status="music_not_configured")

    keyword = _resolve_keyword(request.payload)
    if not keyword:
        return _error_result("请使用“点歌 歌曲名”搜索音乐。", provider_status="keyword_required")
    try:
        song = _search_song(config, keyword=keyword)
    except (PluginHttpError, ValueError):
        return _error_result("音乐服务暂时不可用，请稍后重试。", provider_status="search_failed")
    if song is None:
        message = f"没有找到可播放的歌曲：{keyword}"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=message,
            output={
                "provider_status": "empty",
                "keyword": keyword,
                "replies": standard_replies([text_reply(message)]),
            },
        )

    display_name = f"{song.artist_name} - {song.song_name}" if song.artist_name else song.song_name
    reply_text = f"已找到：{display_name}\n播放地址：{song.play_url}"
    file_name = _build_audio_file_name(song)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已找到歌曲 {song.song_name}。",
        output={
            "provider_status": "completed",
            "keyword": keyword,
            "song_id": song.song_id,
            "song_name": song.song_name,
            "artist_name": song.artist_name,
            "cover_url": song.cover_url,
            "replies": standard_replies(
                [
                    text_reply(reply_text),
                    file_reply(file_name=file_name, file_url=song.play_url),
                ]
            ),
        },
    )


def _search_song(config: MusicConfig, *, keyword: str) -> SongResult | None:
    """固定使用 Search 和 Song_V1 两个业务接口，不接受任意 URL 模板。"""

    client = PluginHttpClient()
    search_payload = _request_json(
        client,
        config,
        "/Search",
        {"keyword": keyword, "limit": 10},
    )
    songs = search_payload.get("data")
    if not isinstance(songs, list) or not songs or not isinstance(songs[0], Mapping):
        return None
    first_song = songs[0]
    song_id = str(first_song.get("id") or "").strip()
    if not song_id:
        return None

    detail_payload = _request_json(
        client,
        config,
        "/Song_V1",
        {"url": song_id, "level": config.quality, "type": "json"},
    )
    detail = detail_payload.get("data")
    if not isinstance(detail, Mapping):
        return None
    play_url = _public_http_url(detail.get("url"))
    if not play_url:
        return None
    return SongResult(
        song_id=song_id,
        song_name=_inline_text(detail.get("name") or first_song.get("name") or keyword),
        artist_name=_inline_text(
            detail.get("ar_name")
            or first_song.get("artist_string")
            or _normalize_artists(first_song.get("artists"))
        ),
        play_url=play_url,
        cover_url=_public_http_url(detail.get("pic") or first_song.get("picUrl")),
    )


def _request_json(
    client: PluginHttpClient,
    config: MusicConfig,
    path: str,
    parameters: Mapping[str, Any],
) -> Mapping[str, Any]:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    response = client.request(
        "GET",
        f"{config.api_base}{path}?{urlencode({key: str(value) for key, value in parameters.items()})}",
        headers=headers,
        connect_timeout_seconds=min(8, config.timeout_seconds),
        total_timeout_seconds=config.timeout_seconds,
        max_response_bytes=2 * 1024 * 1024,
        max_redirects=2,
        retry_count=1,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"music http {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("music response invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("music response must be object")
    if payload.get("success") is not True or int(payload.get("status") or 0) != 200:
        raise ValueError("music service rejected request")
    return payload


def _resolve_keyword(payload: Mapping[str, Any]) -> str:
    explicit = str(payload.get("keyword") or payload.get("command_args") or "").strip()
    if explicit:
        return _inline_text(explicit)
    for key in ("content", "message_text", "query"):
        normalized = _strip_command(str(payload.get(key) or ""))
        if normalized:
            return normalized
    return ""


def _strip_command(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    for command in COMMANDS:
        if normalized == command:
            return ""
        if normalized.startswith(f"{command} "):
            return normalized[len(command) :].strip()
    return normalized


def _build_audio_file_name(song: SongResult) -> str:
    display_name = f"{song.artist_name} - {song.song_name}" if song.artist_name else song.song_name
    # 文件名会进入多个渠道适配器，统一清除路径分隔符和控制字符，避免把歌曲名
    # 解释为目录或协议载荷。平台回复层还会再次检查最大长度。
    safe_name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", display_name).strip(" ._")
    return f"{(safe_name or 'music')[:240]}.mp3"


def _normalize_artists(value: Any) -> str:
    if isinstance(value, list):
        names = []
        for item in value:
            if isinstance(item, Mapping):
                name = _inline_text(item.get("name"))
            else:
                name = _inline_text(item)
            if name:
                names.append(name)
        return " / ".join(names)
    return _inline_text(value)


def _public_http_url(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    # file_reply 会执行最终 URL 校验；这里先过滤明显的非 HTTP 地址，便于把无效
    # 播放结果按“未找到”处理，而不是在构造回复时暴露内部异常。
    parsed = urlsplit(normalized)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return normalized


def _inline_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _error_result(message: str, *, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _load_config(raw_config: Mapping[str, Any]) -> MusicConfig:
    raw = dict(raw_config)
    quality = str(raw.get("quality") or "exhigh").strip().lower()
    if quality not in {"standard", "higher", "exhigh", "lossless", "hires"}:
        quality = "exhigh"
    return MusicConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        quality=quality,
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=15, minimum=5, maximum=60),
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
