from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import Conversation
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import file_reply, standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore


STATE_NAMESPACE = "guess_song"
STATE_KEY_PREFIX = "conversation"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; MultiBotSaaS-GuessSong/2.0)"
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


@dataclass(frozen=True)
class GuessSongConfig:
    """租户自备音乐服务及游戏时限配置。"""

    api_base: str
    api_key: str
    quality: str
    timeout_seconds: int
    hint_seconds: int
    game_ttl_seconds: int
    popular_singers: tuple[str, ...]


@dataclass(frozen=True)
class SongResult:
    """猜歌状态需要保存的最小歌曲信息。"""

    song_id: str
    song_name: str
    artist_name: str
    play_url: str
    cover_url: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只处理重载，游戏状态必须绑定可信租户会话。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="猜歌名插件已就绪。",
            output={
                "api_base_configured": bool(config.api_base),
                "api_key_configured": bool(config.api_key),
                "quality": config.quality,
                "hint_seconds": config.hint_seconds,
                "game_ttl_seconds": config.game_ttl_seconds,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("猜歌名只能通过真实群聊会话执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在当前群读取或更新一局游戏，不接受载荷指定其他会话。"""

    if request.action != "execute":
        return execute_action(request)
    context, conversation, error = _require_group_context(db, request.context)
    if error is not None or context is None or conversation is None:
        return error or _error_result("猜歌名缺少可信群聊上下文。", "trusted_context_required")
    config = _load_config(request.config)
    if not config.api_base:
        return _error_result("请先配置当前租户的音乐服务地址。", "music_not_configured")

    store = PluginStateStore(db, context)
    state_key = _state_key(context)
    current_game = store.get(STATE_NAMESPACE, state_key)
    arguments = _inline_text(request.payload.get("command_args"))
    if current_game:
        if arguments == "下一首":
            return _start_round(
                store,
                state_key=state_key,
                config=config,
                singer_name=None,
                previous_game=current_game,
            )
        if not arguments:
            return _text_result("当前已有一局进行中，请发送“猜歌名 歌名”作答，或发送“猜歌名 下一首”。", "playing")
        return _answer_round(
            store,
            state_key=state_key,
            config=config,
            current_game=current_game,
            answer=arguments,
        )
    if arguments == "下一首":
        return _text_result("当前没有进行中的猜歌游戏，请先发送“猜歌名”。", "empty")
    return _start_round(
        store,
        state_key=state_key,
        config=config,
        singer_name=arguments or None,
        previous_game=None,
    )


def _start_round(
    store: PluginStateStore,
    *,
    state_key: str,
    config: GuessSongConfig,
    singer_name: str | None,
    previous_game: Mapping[str, Any] | None,
) -> PluginExecutionResult:
    """从租户音乐服务随机选歌，覆盖当前群的旧局状态。"""

    try:
        song = _search_random_song(config, singer_name=singer_name)
    except (PluginHttpError, ValueError):
        return _error_result("音乐服务暂时不可用，请稍后重试。", "music_service_failed")
    if song is None:
        target = singer_name or "热门歌手"
        return _text_result(f"没有找到“{target}”的可播放歌曲，请换个歌手再试。", "empty")

    game = {
        "song_id": song.song_id,
        "song_name": song.song_name,
        "artist_name": song.artist_name,
        "play_url": song.play_url,
        "cover_url": song.cover_url,
        "started_at": int(now_shanghai().timestamp()),
    }
    store.put(
        STATE_NAMESPACE,
        state_key,
        game,
        ttl_seconds=config.game_ttl_seconds,
    )

    replies: list[dict[str, Any]] = []
    if previous_game:
        previous_name = _song_display_name(previous_game)
        replies.extend(
            [
                text_reply(f"上一首答案：{previous_name}"),
                file_reply(
                    file_name=_answer_file_name(previous_game),
                    file_url=str(previous_game.get("play_url") or ""),
                ),
            ]
        )
    prompt = (
        "猜歌名开始。请试听音频后发送“猜歌名 歌名”作答，发送“猜歌名 下一首”可跳过。\n"
        f"歌手：{song.artist_name or '未知'}"
    )
    replies.extend(
        [
            file_reply(file_name="guess-song-preview.mp3", file_url=song.play_url),
            text_reply(prompt),
        ]
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="新一轮猜歌名已开始。",
        output={
            "provider_status": "next_round" if previous_game else "started",
            "has_previous_answer": bool(previous_game),
            # 歌名只保存在绑定状态表，执行输出不记录答案或完整上游响应。
            "replies": standard_replies(replies),
        },
    )


def _answer_round(
    store: PluginStateStore,
    *,
    state_key: str,
    config: GuessSongConfig,
    current_game: Mapping[str, Any],
    answer: str,
) -> PluginExecutionResult:
    """校验答案；答对后删除状态，答错后按时间决定是否给出提示。"""

    song_name = _inline_text(current_game.get("song_name"))
    if not song_name or not _public_http_url(current_game.get("play_url")):
        store.delete(STATE_NAMESPACE, state_key)
        return _error_result("本局歌曲已失效，请重新发送“猜歌名”。", "state_invalid")
    if _answer_matches(answer, song_name):
        store.delete(STATE_NAMESPACE, state_key)
        display_name = _song_display_name(current_game)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="猜歌名回答正确。",
            output={
                "provider_status": "correct",
                "replies": standard_replies(
                    [
                        text_reply(f"答对了，答案是：{display_name}"),
                        file_reply(
                            file_name=_answer_file_name(current_game),
                            file_url=str(current_game.get("play_url") or ""),
                        ),
                    ]
                ),
            },
        )

    started_at = _bounded_int(current_game.get("started_at"), default=0, minimum=0, maximum=4_102_444_800)
    elapsed = max(0, int(now_shanghai().timestamp()) - started_at) if started_at else 0
    if elapsed >= config.hint_seconds:
        artist = _inline_text(current_game.get("artist_name")) or "未知歌手"
        hint = f"提示：歌手是 {artist}，歌名是 {_mask_song_name(song_name)}。"
        return _text_result(f"还不对。{hint}", "incorrect_with_hint")
    remaining = max(config.hint_seconds - elapsed, 1)
    return _text_result(f"还不对，继续猜吧。{remaining} 秒后可获得提示。", "incorrect")


def _search_random_song(config: GuessSongConfig, *, singer_name: str | None) -> SongResult | None:
    """固定调用 Search 与 Song_V1，不接受租户配置任意接口模板。"""

    keyword = singer_name or random.choice(config.popular_singers)
    client = PluginHttpClient()
    search_payload = _request_json(
        client,
        config,
        "/Search",
        {"keywords": keyword, "limit": 10},
    )
    raw_songs = search_payload.get("data")
    if not isinstance(raw_songs, list):
        return None
    candidates = [item for item in raw_songs if isinstance(item, Mapping) and str(item.get("id") or "").strip()]
    if not candidates:
        return None
    selected = random.choice(candidates)
    song_id = str(selected.get("id") or "").strip()
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
    song_name = _inline_text(detail.get("name") or selected.get("name"))
    if not song_name:
        return None
    return SongResult(
        song_id=song_id,
        song_name=song_name,
        artist_name=_inline_text(
            detail.get("ar_name")
            or selected.get("artist_string")
            or _normalize_artists(selected.get("artists"))
            or keyword
        ),
        play_url=play_url,
        cover_url=_public_http_url(detail.get("pic") or selected.get("picUrl")),
    )


def _request_json(
    client: PluginHttpClient,
    config: GuessSongConfig,
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


def _require_group_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[PluginExecutionContext | None, Conversation | None, PluginExecutionResult | None]:
    """验证当前群会话主键，状态键不从命令载荷读取。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.chat_type != "group"
    ):
        return None, None, _error_result("请在群聊中使用猜歌名。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
        or conversation.chat_type != "group"
    ):
        return None, None, _error_result("当前群聊上下文无效。", "trusted_context_required")
    return context, conversation, None


def _state_key(context: PluginExecutionContext) -> str:
    return f"{STATE_KEY_PREFIX}:{int(context.conversation_id or 0)}"


def _answer_matches(answer: str, song_name: str) -> bool:
    normalized_answer = _normalize_answer(answer)
    normalized_song = _normalize_answer(song_name)
    if not normalized_answer or not normalized_song:
        return False
    return normalized_answer == normalized_song or (
        len(normalized_answer) >= 2 and normalized_answer in normalized_song
    )


def _normalize_answer(value: str) -> str:
    return re.sub(r"[\s\-—_·,，.。!！?？:：()（）\[\]【】]+", "", str(value or "").strip().lower())


def _mask_song_name(song_name: str) -> str:
    normalized = _inline_text(song_name)
    if len(normalized) <= 1:
        return normalized
    return normalized[0] + "*" * (len(normalized) - 1)


def _song_display_name(song: Mapping[str, Any]) -> str:
    song_name = _inline_text(song.get("song_name")) or "未知歌曲"
    artist_name = _inline_text(song.get("artist_name"))
    return f"{artist_name} - {song_name}" if artist_name else song_name


def _answer_file_name(song: Mapping[str, Any]) -> str:
    safe_name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", _song_display_name(song)).strip(" ._")
    return f"{(safe_name or 'guess-song-answer')[:240]}.mp3"


def _normalize_artists(value: Any) -> str:
    if not isinstance(value, list):
        return _inline_text(value)
    names: list[str] = []
    for item in value:
        name = _inline_text(item.get("name")) if isinstance(item, Mapping) else _inline_text(item)
        if name:
            names.append(name)
    return " / ".join(names)


def _public_http_url(value: Any) -> str:
    normalized = str(value or "").strip()
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


def _load_config(raw: Mapping[str, Any]) -> GuessSongConfig:
    quality = str(raw.get("quality") or "exhigh").strip().lower()
    if quality not in {"standard", "higher", "exhigh", "lossless", "hires"}:
        quality = "exhigh"
    raw_singers = raw.get("popular_singers")
    if isinstance(raw_singers, (list, tuple)):
        singers = tuple(_inline_text(item) for item in raw_singers if _inline_text(item))
    else:
        singers = ()
    return GuessSongConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        quality=quality,
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=15, minimum=5, maximum=60),
        hint_seconds=_bounded_int(raw.get("hint_seconds"), default=30, minimum=10, maximum=300),
        game_ttl_seconds=_bounded_int(raw.get("game_ttl_seconds"), default=1800, minimum=300, maximum=86400),
        popular_singers=singers or DEFAULT_SINGERS,
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _text_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
