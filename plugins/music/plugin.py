from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.tenant_context import require_tenant_id
from app.models.entities import BotPlugin, Channel, Conversation, PluginStateRecord
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.plugin_runtime.replies import file_reply, music_reply, standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore
from plugins.music.service import (
    FILE_TYPES, MAX_KEYWORD_LENGTH, MusicApi, MusicConfig, MusicError, PlayableSong, Song,
    http_url, identifier, inline_text, request_guard, resolve_card_source,
)


logger = get_logger(__name__)
PLAY_COMMANDS = ("点歌", "音乐", "音乐点播", "点播音乐", "音乐点歌")
COMMANDS = (*PLAY_COMMANDS, "搜歌", "选歌", "歌词")
STATE_NAMESPACE = "music_selection"
MAX_SELECTIONS = 256
FAILURES = {
    "auth": "音乐服务授权失效，请管理员检查 QQMusicApi 的账号登录及可选的反向代理密钥。",
    "access_denied": "音乐服务拒绝访问，请管理员检查服务访问策略或网关授权。",
    "limited": "上游音乐接口暂时受限，请稍后再试。",
    "quota": "音乐服务的共享请求额度已用完，请稍后再试。",
    "unplayable": "这首歌暂时没有播放权限或可用地址，可能需要会员或单独购买。可用“搜歌 歌名”选择其他版本。",
    "vkey_failed": "这首歌的播放授权暂时获取失败，请稍后再试或选择其他版本。",
    "device": "QQ 音乐限制了当前播放设备，请管理员检查音乐账号的设备状态。",
    "incompatible": "音乐服务响应或接口不兼容，请管理员确认使用 QQMusicApi 0.9.0 Web 服务。",
    "configuration_error": "音乐配置不正确，请检查 QQMusicApi 服务地址、音质、回复方式及卡片来源 App ID。",
    "timeout": "音乐请求已超时，请稍后再试。",
    "upstream": "音乐服务暂时不可用，请稍后再试。",
    "pending": "上一条音乐请求还在处理中，请稍等。",
    "busy": "音乐服务繁忙，请稍后再试。",
}


@dataclass(frozen=True)
class Command:
    operation: str
    keyword: str = ""
    index: int = 1


@dataclass
class SelectionSession:
    db: Session
    context: PluginExecutionContext
    channel: Channel
    binding: BotPlugin
    store: PluginStateStore
    state_key: str
    account_fingerprint: str
    service_fingerprint: str
    config: MusicConfig

    def clear(self) -> None:
        self.store.delete(STATE_NAMESPACE, self.state_key)

    def save(self, songs: list[Song]) -> None:
        if not songs:
            return
        self.store.cleanup_expired(limit=100)
        rows = self.db.scalars(select(PluginStateRecord).where(
            PluginStateRecord.tenant_id == self.context.tenant_id,
            PluginStateRecord.bot_plugin_id == self.context.binding_id,
            PluginStateRecord.namespace == STATE_NAMESPACE,
        ).order_by(PluginStateRecord.updated_at, PluginStateRecord.id).limit(MAX_SELECTIONS + 1)).all()
        for record in rows[:max(0, len(rows) - MAX_SELECTIONS + 1)]:
            self.db.delete(record)
        self.store.put(STATE_NAMESPACE, self.state_key, {
            "service": self.service_fingerprint,
            "songs": [asdict(song) for song in songs],
        }, ttl_seconds=self.config.selection_timeout_seconds)

    def load(self) -> list[Song]:
        state = self.store.get(STATE_NAMESPACE, self.state_key)
        if not state or state.get("service") != self.service_fingerprint:
            return []
        values = state.get("songs")
        if not isinstance(values, list):
            return []
        songs = []
        for value in values[:10]:
            if not isinstance(value, Mapping) or not identifier(value.get("mid")) or not inline_text(value.get("title")):
                return []
            songs.append(Song(
                mid=value["mid"], title=inline_text(value["title"]), singer=inline_text(value.get("singer"), 300),
                album_mid=identifier(value.get("album_mid")), singer_mid=identifier(value.get("singer_mid")),
                media_mid=identifier(value.get("media_mid")),
                song_type=value.get("song_type") if type(value.get("song_type")) is int and value["song_type"] >= 0 else None,
                needs_detail=value.get("needs_detail") is True,
            ))
        return songs

    def is_current(self) -> bool:
        self.db.refresh(self.channel, ["runtime_info_json", "is_enabled", "deleted_at"])
        self.db.refresh(self.binding, ["status"])
        return bool(self.channel.is_enabled and self.channel.deleted_at is None and self.binding.status == "enabled"
                    and _account_fingerprint(self.channel) == self.account_fingerprint)


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """Stateless/manual entry; retained selections require a trusted message context."""
    return _execute(request)


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action != "execute":
        return execute_action(request)
    context = request.context
    if (context is None or not context.channel_id or not context.conversation_id or not context.sender_id
            or context.tenant_id != require_tenant_id()):
        return _text_result("音乐会话缺少可信身份，请通过真实聊天消息使用。", "trusted_context_required", ok=False)
    channel = db.get(Channel, context.channel_id)
    conversation = db.get(Conversation, context.conversation_id)
    binding = db.get(BotPlugin, context.binding_id)
    if (channel is None or conversation is None or binding is None
            or channel.tenant_id != context.tenant_id or channel.bot_id != context.bot_id
            or conversation.tenant_id != context.tenant_id or conversation.bot_id != context.bot_id
            or conversation.channel_id != channel.id
            or binding.tenant_id != context.tenant_id or binding.bot_id != context.bot_id
            or binding.status != "enabled" or not channel.is_enabled or channel.deleted_at is not None):
        return _text_result("当前音乐会话或插件绑定无效。", "trusted_context_required", ok=False)
    try:
        config = _load_config(request.config)
    except MusicError as exc:
        return _failure(exc)
    account = _account_fingerprint(channel)
    session = SelectionSession(
        db=db, context=context, channel=channel, binding=binding, store=PluginStateStore(db, context),
        state_key="selection:" + _digest([context.bot_id, channel.id, conversation.id, context.sender_id, account]),
        account_fingerprint=account,
        service_fingerprint=_digest([config.api_base, config.file_type, config.api_key]), config=config,
    )
    return _execute(request, config=config, session=session)


def _execute(request: PluginExecutionRequest, *, config: MusicConfig | None = None,
             session: SelectionSession | None = None) -> PluginExecutionResult:
    try:
        config = config or _load_config(request.config)
        if request.action == "reload":
            if config.api_base:
                MusicApi(config)
            return PluginExecutionResult(ok=True, status="running", message="音乐点播配置已加载。", output={
                "upstream": "QQMusicApi", "upstream_version": "0.9.0",
                "api_base_configured": bool(config.api_base), "api_key_configured": bool(config.api_key),
                "file_type": config.file_type,
            })
        if request.action != "execute":
            return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
        command = _parse_command(request.payload)
        if command.operation != "select" and not command.keyword:
            example = {"play": "点歌 晴天", "search": "搜歌 晴天", "lyric": "歌词 晴天"}[command.operation]
            return _text_result(f"请带上歌名或歌手，例如“{example}”。", "keyword_required", ok=False)
        if command.operation == "select" and session is None:
            return _text_result("选歌需要真实聊天会话，请先在聊天中发送“搜歌 歌名”。", "trusted_context_required", ok=False)
        if not config.api_base:
            return _text_result("请先配置当前租户的 QQMusicApi 0.9.0 服务地址。", "music_not_configured", ok=False)
        context = request.context
        identity = session.state_key if session else _digest([getattr(context, "sender_id", None), command.keyword])
        with request_guard(getattr(context, "tenant_id", 0), getattr(context, "binding_id", 0), identity):
            result = _run_command(MusicApi(config, PluginHttpClient()), command, session)
            if session is not None and not session.is_current():
                return PluginExecutionResult(ok=False, status="error", message="音乐请求的账号状态已改变。",
                                             output={"provider_status": "context_changed", "replies": []})
            return result
    except MusicError as exc:
        if session is not None and not session.is_current():
            return PluginExecutionResult(ok=False, status="error", message="音乐请求的账号状态已改变。",
                                         output={"provider_status": "context_changed", "replies": []})
        return _failure(exc, config)


def _run_command(api: MusicApi, command: Command, session: SelectionSession | None) -> PluginExecutionResult:
    if command.operation == "select":
        songs = session.load() if session else []
        if not songs:
            return _text_result("选歌列表已过期或不存在，请先“搜歌 歌名”。", "selection_expired")
    else:
        if session is not None and command.operation in {"play", "search"}:
            # A new failed or empty search must not leave the previous candidates selectable.
            session.clear()
        songs = api.search(command.keyword)
        if session is not None and command.operation in {"play", "search"}:
            session.save(songs)
        if not songs:
            return _text_result(f"没有搜到“{command.keyword}”，请换个关键词。", "empty")
    if command.operation == "search":
        lines = [f"{index}. {song.title}" + (f" — {song.singer}" if song.singer else "")
                 for index, song in enumerate(songs, 1)]
        hint = (f"{api.config.selection_timeout_seconds} 秒内发送“选歌 序号”，例如“选歌 2”。" if session
                else "可使用“点歌 歌名 -n 序号”选择版本。")
        return _text_result("\n".join([*lines, hint]), "candidates")
    if command.index > len(songs):
        return _text_result(f"本次有 {len(songs)} 首歌，请输入 1～{len(songs)} 之间的序号。", "invalid_index", ok=False)
    song = songs[command.index - 1]
    if command.operation == "lyric":
        lyric = api.lyric(song)
        lyric = re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d+)?\]", "", lyric).strip()
        return _text_result(f"{song.title} — {song.singer}\n{lyric}" if lyric else "这首歌暂无歌词。", "lyric")
    return _play_result(api.resolve(song), api.config, command.keyword)


def _play_result(result: PlayableSong, config: MusicConfig, keyword: str) -> PluginExecutionResult:
    song = result.song
    display_name = f"{song.singer} - {song.title}" if song.singer else song.title
    text = f"已找到：{display_name}\n歌曲页面：{song.page_url}\n播放地址：{result.play_url}"
    if config.output_mode == "file":
        replies = [text_reply(text), file_reply(file_name=_build_audio_file_name(result), file_url=result.play_url)]
    elif config.output_mode == "text":
        replies = [text_reply(text)]
    else:
        replies = [music_reply(
            title=song.title, desc=song.singer, url=song.page_url, data_url=result.play_url,
            song_album_url=song.cover_url, song_lyric=result.lyric, app_id=config.app_id, app_name=config.app_name,
        )]
    return PluginExecutionResult(ok=True, status="completed", message=f"已找到歌曲 {song.title}。", output={
        "provider_status": "completed", "keyword": keyword, "song_id": song.mid,
        "song_name": song.title, "artist_name": song.singer, "cover_url": song.cover_url,
        "replies": standard_replies(replies),
    })


def _parse_command(payload: Mapping[str, Any]) -> Command:
    content = next((value.strip() for key in ("content", "message_text", "query")
                    if isinstance(value := payload.get(key), str) and value.strip()), "")
    content = re.sub(r"^[#/]\s*", "", content)
    name = inline_text(payload.get("command_name"))
    matched = next((command for command in sorted(COMMANDS, key=len, reverse=True)
                    if content == command or re.match(re.escape(command) + r"\s", content)), "")
    if not name:
        name = matched
    explicit = payload.get("keyword")
    if isinstance(explicit, str) and explicit.strip():
        arguments = explicit
    elif isinstance(payload.get("command_args"), str):
        arguments = payload["command_args"]
    else:
        arguments = content[len(matched):] if matched else content
    operation = {"搜歌": "search", "选歌": "select", "歌词": "lyric"}.get(name, "play")
    tokens = arguments.split()
    index = 1
    if operation == "select":
        if len(tokens) != 1 or not re.fullmatch(r"[0-9]{1,9}", tokens[0]) or int(tokens[0]) < 1:
            raise MusicError("invalid_index", "command")
        return Command(operation=operation, index=int(tokens[0]))
    if operation == "play" and "-n" in tokens:
        position = tokens.index("-n")
        if tokens.count("-n") != 1 or position + 1 >= len(tokens) or not re.fullmatch(r"[0-9]{1,9}", tokens[position + 1]):
            raise MusicError("invalid_index", "command")
        index = int(tokens[position + 1])
        if index < 1:
            raise MusicError("invalid_index", "command")
        del tokens[position:position + 2]
    return Command(operation=operation, keyword=inline_text(" ".join(tokens), MAX_KEYWORD_LENGTH), index=index)


def _load_config(raw: Mapping[str, Any]) -> MusicConfig:
    file_type = str(raw.get("file_type") or "mp3_128").strip().lower()
    output_mode = str(raw.get("output_mode") or "auto").strip().lower()
    on_failure = str(raw.get("on_failure") or "message").strip().lower()
    if file_type not in FILE_TYPES or output_mode not in {"auto", "file", "text"} or on_failure not in {"message", "silent"}:
        raise MusicError("configuration_error", "config")
    app_id, app_name = resolve_card_source(raw.get("app_id"))
    return MusicConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(), file_type=file_type,
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), 25, 5, 25),
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), 8, 1, 15),
        result_limit=_bounded_int(raw.get("result_limit"), 5, 1, 10),
        selection_timeout_seconds=_bounded_int(raw.get("selection_timeout_seconds"), 180, 30, 600),
        include_lyrics=str(raw.get("include_lyrics", True)).strip().lower() not in {"false", "0", "no", "off"},
        output_mode=output_mode, on_failure=on_failure,
        app_id=app_id, app_name=app_name,
    )


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError, OverflowError):
        return default


def _account_fingerprint(channel: Channel) -> str:
    try:
        info = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError):
        info = {}
    bridge = info.get("webridge") if isinstance(info, Mapping) else None
    bridge = bridge if isinstance(bridge, Mapping) else {}
    return _digest([channel.id, channel.channel_type, *(bridge.get(key) for key in ("device_id", "account_id", "session_id"))])


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()


def _build_audio_file_name(result: PlayableSong) -> str:
    display_name = f"{result.song.singer} - {result.song.title}" if result.song.singer else result.song.title
    safe_name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", display_name).strip(" ._")
    return f"{(safe_name or 'music')[:240]}{result.extension}"


def _public_http_url(value: Any) -> str:
    return http_url(value)


def _text_result(message: str, provider_status: str, *, ok: bool = True) -> PluginExecutionResult:
    return PluginExecutionResult(ok=ok, status="completed" if ok else "error", message=message, output={
        "provider_status": provider_status, "replies": standard_replies([text_reply(message)]),
    })


def _failure(error: MusicError, config: MusicConfig | None = None) -> PluginExecutionResult:
    message = ("请使用正整数序号，例如“选歌 2”或“点歌 晴天 -n 2”。" if error.kind == "invalid_index"
               else FAILURES.get(error.kind, FAILURES["upstream"]))
    logger.warning("music request failed", extra={"stage": error.stage, "kind": error.kind, "http_status": error.http_status})
    return PluginExecutionResult(ok=False, status="error", message=message, output={
        "provider_status": error.kind, "failure_stage": error.stage, "http_status": error.http_status,
        "replies": [] if config and config.on_failure == "silent" else standard_replies([text_reply(message)]),
    })
