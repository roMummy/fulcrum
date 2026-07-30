from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import inspect
import json
import logging
import mimetypes
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import parse as urlparse
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


APP_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = APP_ROOT / "config.json"
LOG_PATH = APP_ROOT / "provider.log"
DOWNLOAD_ROOT = APP_ROOT / "downloads"
OUTBOUND_CACHE_ROOT = APP_ROOT / "outbound_cache"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("wechat_winauto_provider")


class ProviderConfig(BaseModel):
    provider_host: str = "0.0.0.0"
    provider_port: int = 8060
    callback_url: str = ""
    callback_secret: str = ""
    callback_timeout_seconds: int = DEFAULT_CALLBACK_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    listen_chats: list[str] = Field(default_factory=list)
    auto_expand_listen: bool = True
    auto_expand_scan_interval_seconds: float = 30.0
    auto_expand_max_chats: int = 200
    chat_alias_map: dict[str, str] = Field(default_factory=dict)
    self_name: str = ""
    provider_token: str = ""
    fail_on_missing_wxauto: bool = False
    download_media: bool = True
    media_download_dir: str = str(DOWNLOAD_ROOT)
    media_download_async: bool = True
    media_download_initial_delay_seconds: float = 1.5
    media_download_retry_count: int = 2
    media_download_retry_delay_seconds: float = 2.0
    startup_retry_seconds: float = 5.0
    poll_error_retry_seconds: float = 3.0
    debug_dump_raw_messages: bool = True
    debug_dump_dir: str = str(APP_ROOT / "debug_messages")
    debug_recent_message_limit: int = 50
    fallback_poll_strategy: str = "disabled"
    fallback_poll_chat: str = ""


class SendMessageRequest(BaseModel):
    channel_id: int | None = None
    channel_type: str | None = None
    conversation_id: int | None = None
    conversation_external_id: str | None = None
    receiver_id: str | None = None
    conversation_title: str | None = None
    chat_type: str | None = "direct"
    message_type: str = "text"
    content: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ListenStatus(BaseModel):
    running: bool
    receive_mode: str = "listener_only"
    listener_api: str | None = None
    fallback_poll_enabled: bool = False
    supported_receive_apis: list[str] = Field(default_factory=list)
    last_receive_source: str | None = None
    last_poll_at: str | None = None
    last_callback_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    listened_chats: list[str] = Field(default_factory=list)
    seed_chats: list[str] = Field(default_factory=list)
    auto_discovered_chats: list[str] = Field(default_factory=list)
    listen_failures: dict[str, str] = Field(default_factory=dict)
    wxauto_ready: bool = False
    pending_messages: int = 0
    startup_attempts: int = 0
    consecutive_failures: int = 0
    delivered_message_count: int = 0
    callback_success_count: int = 0
    callback_failure_count: int = 0
    last_message_at: str | None = None
    last_message_preview: str | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_provider_timestamp(value: Any) -> tuple[str, str | None]:
    raw_text = str(value).strip() if value not in (None, "") else ""
    if not raw_text:
        return utc_now_iso(), None

    local_now = datetime.now().astimezone()
    local_tz = local_now.tzinfo or timezone.utc

    if isinstance(value, datetime):
        dt_value = value if value.tzinfo else value.replace(tzinfo=local_tz)
        return dt_value.astimezone(timezone.utc).isoformat(), raw_text

    if isinstance(value, (int, float)) or raw_text.isdigit():
        try:
            normalized = float(value)
            if normalized > 10_000_000_000:
                normalized = normalized / 1000
            dt_value = datetime.fromtimestamp(normalized, tz=timezone.utc)
            return dt_value.isoformat(), raw_text
        except (TypeError, ValueError, OverflowError, OSError):
            return utc_now_iso(), raw_text

    normalized_text = raw_text.replace("/", "-").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(timezone.utc).isoformat(), raw_text
    except ValueError:
        pass

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed_time = datetime.strptime(raw_text, fmt).time()
            parsed = local_now.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=parsed_time.second,
                microsecond=0,
            )
            return parsed.astimezone(timezone.utc).isoformat(), raw_text
        except ValueError:
            continue

    return utc_now_iso(), raw_text


def load_config() -> ProviderConfig:
    if not CONFIG_PATH.exists():
        logger.warning("config.json not found, using defaults")
        return ProviderConfig()
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return ProviderConfig.model_validate(raw if isinstance(raw, dict) else {})


def safe_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): safe_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_jsonable(item) for item in value]
    if hasattr(value, "__dict__"):
        data = {}
        for key, item in vars(value).items():
            if key.startswith("_"):
                continue
            data[str(key)] = safe_jsonable(item)
        if data:
            data["_type"] = type(value).__name__
            return data
    return str(value)


def build_message_log_payload(message: "NormalizedMessage") -> dict[str, Any]:
    details = message.details if isinstance(message.details, dict) else {}
    return {
        "event_key": message.event_key,
        "conversation_title": message.conversation_title,
        "conversation_external_id": message.conversation_external_id,
        "chat_type": message.chat_type,
        "sender_name": message.sender_name,
        "message_type": message.message_type,
        "content_preview": message.content[:120],
        "timestamp": message.timestamp,
        "source_timestamp": details.get("source_timestamp"),
        "provider_message_type": details.get("provider_message_type"),
        "provider_payload_type": details.get("provider_payload_type"),
        "is_self": details.get("is_self"),
    }


def _looks_like_download_failure(result: Any) -> tuple[bool, str | None]:
    if result is None:
        return True, "download returned None"
    if isinstance(result, dict):
        status = str(result.get("status") or "").strip()
        message = str(result.get("message") or "").strip() or None
        if status in {"失败", "error", "failed"}:
            return True, message or status
    normalized = str(result).strip()
    if not normalized:
        return True, "download returned empty result"
    lowered = normalized.lower()
    if "失败" in normalized or "超时" in normalized or "error" in lowered or "failed" in lowered or "timeout" in lowered:
        return True, normalized
    return False, None


def _list_download_dir_files(directory: Path) -> dict[str, float]:
    files: dict[str, float] = {}
    if not directory.exists():
        return files
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        try:
            files[str(entry.resolve())] = entry.stat().st_mtime
        except OSError:
            continue
    return files


def _coerce_existing_path(value: Any, *, base_dir: Path | None = None) -> Path | None:
    if value in (None, ""):
        return None
    candidates: list[Path] = []
    if isinstance(value, Path):
        candidates.append(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        candidates.append(Path(text))
        if base_dir is not None:
            candidates.append(base_dir / text)
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate.resolve()
        except OSError:
            continue
    return None


def sign_payload(secret: str, body: bytes, timestamp: str) -> str:
    message = f"{timestamp}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def assert_provider_authorized(request: Request) -> None:
    if not config.provider_token:
        return
    provided = str(request.headers.get("X-Provider-Token") or "").strip()
    if not provided:
        authorization = str(request.headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(config.provider_token, provided):
        raise HTTPException(status_code=401, detail="invalid_provider_token")


@dataclass
class NormalizedMessage:
    event_key: str
    conversation_external_id: str
    conversation_title: str
    chat_type: str
    sender_id: str | None
    sender_name: str | None
    content: str
    message_type: str
    timestamp: str
    details: dict[str, Any]

    def to_webhook_payload(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "event_type": "message",
            "conversation": {
                "external_id": self.conversation_external_id,
                "title": self.conversation_title,
                "chat_type": self.chat_type,
            },
            "message": {
                "content": self.content,
                "sender_id": self.sender_id,
                "sender_name": self.sender_name,
                "message_type": self.message_type,
                "external_message_id": None,
            },
            "details": self.details | {"timestamp": self.timestamp},
        }


class WxAutoUnavailableError(RuntimeError):
    pass


class WxAutoListenerUnavailableError(RuntimeError):
    pass


class WxAutoClient:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self._wx = None
        self._chat_cache: dict[str, Any] = {}
        self._seed_chats: list[str] = [str(item or "").strip() for item in config.listen_chats if str(item or "").strip()]
        self._auto_discovered_chats: set[str] = set()
        self._listen_failures: dict[str, str] = {}
        self._last_message_fingerprints: set[str] = set()
        self._chat_recent_signatures: dict[str, collections.deque[str]] = {}
        self._pinned_chat_name: str | None = None
        self._wxauto_import_error: str | None = None
        self._last_receive_source: str | None = None
        self._listener_mode: str | None = None
        self._listener_callback_buffer: collections.deque[tuple[str, Any]] = collections.deque(maxlen=500)
        self._media_download_queue: collections.deque[dict[str, Any]] = collections.deque()
        self._media_download_lock = threading.Lock()
        self._media_download_stop = threading.Event()
        self._media_download_thread: threading.Thread | None = None
        self._media_download_callback: Callable[[NormalizedMessage, dict[str, Any]], None] | None = None
        self._last_auto_expand_scan_at: float = 0.0

    @property
    def ready(self) -> bool:
        return self._wx is not None

    def ensure_ready(self) -> None:
        if self._wx is not None:
            return
        try:
            from wxautox4 import WeChat  # type: ignore
            provider_name = "wxautox4"
        except Exception as exc:  # pragma: no cover - depends on Windows env
            try:
                from wxauto4 import WeChat  # type: ignore
                provider_name = "wxauto4"
            except Exception as fallback_exc:  # pragma: no cover - depends on Windows env
                self._wxauto_import_error = str(fallback_exc)
                raise WxAutoUnavailableError(f"wxautox4/wxauto4 unavailable: {fallback_exc}") from fallback_exc

        self._wx = WeChat()
        logger.info("%s WeChat client initialized", provider_name)

    def list_listened_chats(self) -> list[str]:
        return list(self._chat_cache.keys())

    def list_seed_chats(self) -> list[str]:
        return list(self._seed_chats)

    def list_auto_discovered_chats(self) -> list[str]:
        return sorted(self._auto_discovered_chats)

    def listen_failures(self) -> dict[str, str]:
        return dict(self._listen_failures)

    def set_media_download_callback(self, callback: Callable[[NormalizedMessage, dict[str, Any]], None] | None) -> None:
        self._media_download_callback = callback

    def listener_api_name(self) -> str | None:
        if self._listener_mode:
            return self._listener_mode
        if self._wx is None:
            return None
        if hasattr(self._wx, "StartListening") and hasattr(self._wx, "AddListenChat"):
            return "StartListening"
        if hasattr(self._wx, "GetListenMessage"):
            return "GetListenMessage"
        if hasattr(self._wx, "GetNextNewMessage"):
            return "GetNextNewMessage"
        return None

    def fallback_poll_enabled(self) -> bool:
        strategy = str(self.config.fallback_poll_strategy or "").strip().lower()
        return strategy in {"pinned", "rotate", "sessions"}

    def supported_receive_apis(self) -> list[str]:
        if self._wx is None:
            return []
        supported: list[str] = []
        for api_name in ("AddListenChat", "StartListening", "KeepRunning", "GetListenMessage", "GetNextNewMessage", "GetAllMessage"):
            if hasattr(self._wx, api_name):
                supported.append(api_name)
        return supported

    @property
    def last_receive_source(self) -> str | None:
        return self._last_receive_source

    def start_listening(self) -> None:
        self.ensure_ready()
        self._ensure_media_download_worker_started()
        if self._seed_chats:
            self._pinned_chat_name = self._seed_chats[0]
        callback_mode = hasattr(self._wx, "AddListenChat") and hasattr(self._wx, "StartListening")
        for chat_name in self._seed_chats:
            self._ensure_listen_chat(chat_name, use_callback=callback_mode, source="seed", fail_hard=True)
        if self.config.auto_expand_listen:
            self._auto_expand_listen_targets(use_callback=callback_mode)
        if callback_mode:
            self._start_callback_listener()
            self._listener_mode = "StartListening"
        elif hasattr(self._wx, "GetListenMessage"):
            self._listener_mode = "GetListenMessage"
        elif hasattr(self._wx, "GetNextNewMessage"):
            self._listener_mode = "GetNextNewMessage"
        elif self.fallback_poll_enabled() and hasattr(self._wx, "GetAllMessage"):
            self._listener_mode = f"GetAllMessage:{self.config.fallback_poll_strategy}"
        else:
            self._listener_mode = None

    def stop_background_workers(self) -> None:
        self._media_download_stop.set()
        if self._media_download_thread and self._media_download_thread.is_alive():
            self._media_download_thread.join(timeout=5)

    def _ensure_media_download_worker_started(self) -> None:
        if self._media_download_thread and self._media_download_thread.is_alive():
            return
        self._media_download_stop.clear()
        self._media_download_thread = threading.Thread(
            target=self._media_download_loop,
            name="wxauto-media-download",
            daemon=True,
        )
        self._media_download_thread.start()

    def _enqueue_media_download(self, *, item: Any, message: NormalizedMessage) -> None:
        with self._media_download_lock:
            self._media_download_queue.append(
                {
                    "item": item,
                    "message": message,
                    "queued_at": utc_now_iso(),
                }
            )

    def _pop_media_download_task(self) -> dict[str, Any] | None:
        with self._media_download_lock:
            if not self._media_download_queue:
                return None
            return self._media_download_queue.popleft()

    def _media_download_loop(self) -> None:
        while not self._media_download_stop.is_set():
            task = self._pop_media_download_task()
            if task is None:
                time.sleep(0.2)
                continue
            message = task.get("message")
            if not isinstance(message, NormalizedMessage):
                continue
            event_key = str(message.event_key or "").strip()
            message_type = str(message.message_type or "").strip()
            try:
                details = self._download_media_details(
                    task.get("item"),
                    message_type=message_type,
                    event_key=event_key,
                )
                logger.info(
                    "async media download finished | event=%s | type=%s | details=%s",
                    event_key,
                    message_type,
                    json.dumps(safe_jsonable(details), ensure_ascii=False),
                )
                if callable(self._media_download_callback):
                    self._media_download_callback(message, details)
            except Exception as exc:
                logger.warning(
                    "async media download failed | event=%s | type=%s | error=%s",
                    event_key,
                    message_type,
                    exc,
                )

    def _register_listen_chat(self, chat_name: str, *, use_callback: bool) -> None:
        add_listen = getattr(self._wx, "AddListenChat")
        try:
            parameters = inspect.signature(add_listen).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs: dict[str, Any] = {}
        if "nickname" in parameters:
            kwargs["nickname"] = chat_name
        elif "who" in parameters:
            kwargs["who"] = chat_name
        else:
            add_listen(chat_name)
            return
        if "savepic" in parameters:
            kwargs["savepic"] = False
        if use_callback and "callback" in parameters:
            kwargs["callback"] = self._build_listener_callback(chat_name)
        add_listen(**kwargs)

    def _ensure_listen_chat(self, chat_name: str, *, use_callback: bool, source: str, fail_hard: bool = False) -> bool:
        normalized = str(chat_name or "").strip()
        if not normalized:
            return False
        if normalized in self._chat_cache:
            return True
        try:
            self._select_chat(normalized)
            if hasattr(self._wx, "AddListenChat"):
                self._register_listen_chat(normalized, use_callback=use_callback)
            self._chat_cache[normalized] = normalized
            self._listen_failures.pop(normalized, None)
            if source != "seed":
                self._auto_discovered_chats.add(normalized)
            if not self._pinned_chat_name:
                self._pinned_chat_name = normalized
            logger.info("listening chat registered | chat=%s | source=%s", normalized, source)
            return True
        except Exception as exc:  # pragma: no cover - depends on Windows env
            self._listen_failures[normalized] = str(exc)
            logger.warning("failed to register listen chat | chat=%s | source=%s | error=%s", normalized, source, exc)
            if fail_hard:
                raise RuntimeError(f"register listen chat failed for {normalized}: {exc}") from exc
            return False

    def _start_callback_listener(self) -> None:
        start_listening = getattr(self._wx, "StartListening", None)
        if callable(start_listening):
            start_listening()

    def _build_listener_callback(self, chat_name: str):
        def _callback(message: Any) -> None:
            self._listener_callback_buffer.append((chat_name, message))

        return _callback

    def send_message(self, target_name: str, *, message_type: str, content: str) -> dict[str, Any]:
        self.ensure_ready()
        resolved_target = self.resolve_chat_target(target_name)
        self._select_chat(resolved_target)
        if message_type == "text":
            if not hasattr(self._wx, "SendMsg"):
                raise RuntimeError("wxauto WeChat client does not expose SendMsg")
            self._wx.SendMsg(content, who=resolved_target)
            return {
                "ok": True,
                "status": "sent",
                "external_message_id": f"local-{int(time.time() * 1000)}",
                "target": resolved_target,
            }
        if message_type in {"image", "file"}:
            if not hasattr(self._wx, "SendFiles"):
                raise RuntimeError("wxauto WeChat client does not expose SendFiles")
            self._wx.SendFiles(filepath=content, who=resolved_target)
            return {
                "ok": True,
                "status": "sent",
                "external_message_id": f"local-{int(time.time() * 1000)}",
                "target": resolved_target,
                "file_path": content,
            }
        raise ValueError(f"unsupported message_type: {message_type}")

    def poll_messages(self) -> list[NormalizedMessage]:
        self.ensure_ready()
        messages: list[NormalizedMessage] = []
        self._auto_expand_listen_targets(use_callback=hasattr(self._wx, "AddListenChat") and hasattr(self._wx, "StartListening"))
        if self._listener_callback_buffer:
            self._last_receive_source = "listener:callback"
            buffered_items: list[tuple[str, Any]] = []
            while self._listener_callback_buffer:
                buffered_items.append(self._listener_callback_buffer.popleft())
            for chat_name, item in buffered_items:
                converted = self._normalize_single_message(chat_name, item, chat_descriptor={"chat_name": chat_name})
                if converted is None or converted.event_key in self._last_message_fingerprints:
                    continue
                self._last_message_fingerprints.add(converted.event_key)
                messages.append(converted)
        elif hasattr(self._wx, "GetListenMessage"):
            listen_result = self._wx.GetListenMessage()
            self._last_receive_source = "listener:GetListenMessage"
            messages.extend(self._normalize_listen_result(listen_result))
        elif hasattr(self._wx, "GetNextNewMessage"):
            next_result = self._wx.GetNextNewMessage()
            self._last_receive_source = "listener:GetNextNewMessage"
            messages.extend(self._normalize_next_message_result(next_result))
        elif self.fallback_poll_enabled() and hasattr(self._wx, "GetAllMessage"):
            self._last_receive_source = f"fallback:{self.config.fallback_poll_strategy}"
            messages.extend(self._poll_all_messages_from_chats())
        else:  # pragma: no cover - depends on wxauto version
            self._last_receive_source = "unavailable"
            supported = ", ".join(self.supported_receive_apis()) or "none"
            raise WxAutoListenerUnavailableError(
                "wxauto WeChat client does not expose listener receive APIs; "
                f"supported_apis={supported}; fallback GetAllMessage is disabled by config"
            )
        return messages

    def resolve_chat_target(self, target_name: str) -> str:
        normalized = str(target_name or "").strip()
        if not normalized:
            raise ValueError("empty target")
        return self.config.chat_alias_map.get(normalized, normalized)

    def _select_chat(self, chat_name: str) -> None:
        if hasattr(self._wx, "ChatWith"):
            self._wx.ChatWith(chat_name)
            return
        raise RuntimeError("wxauto WeChat client does not expose ChatWith")

    def _normalize_listen_result(self, result: Any) -> list[NormalizedMessage]:
        normalized: list[NormalizedMessage] = []
        if isinstance(result, dict):
            for chat_key, items in result.items():
                chat_descriptor = self._extract_chat_descriptor(chat_key)
                normalized.extend(self._normalize_message_items(chat_descriptor, items))
        elif isinstance(result, list):
            normalized.extend(self._normalize_message_items({}, result))
        return normalized

    def _normalize_next_message_result(self, result: Any) -> list[NormalizedMessage]:
        normalized: list[NormalizedMessage] = []
        if isinstance(result, dict):
            for chat_name, items in result.items():
                normalized.extend(self._normalize_message_items({"chat_name": str(chat_name)}, items))
        elif isinstance(result, list):
            normalized.extend(self._normalize_message_items({}, result))
        return normalized

    def _normalize_message_items(self, chat_descriptor: dict[str, Any], items: Any) -> list[NormalizedMessage]:
        if not isinstance(items, list):
            return []
        normalized: list[NormalizedMessage] = []
        chat_name = str(chat_descriptor.get("chat_name") or "").strip()
        for item in items:
            converted = self._normalize_single_message(chat_name, item, chat_descriptor=chat_descriptor)
            if converted is None:
                continue
            if converted.event_key in self._last_message_fingerprints:
                continue
            self._last_message_fingerprints.add(converted.event_key)
            normalized.append(converted)
        if len(self._last_message_fingerprints) > 5000:
            self._last_message_fingerprints = set(list(self._last_message_fingerprints)[-1000:])
        return normalized

    def _poll_all_messages_from_chats(self) -> list[NormalizedMessage]:
        normalized: list[NormalizedMessage] = []
        chat_names = list(self._chat_cache.keys()) or list(self._seed_chats)
        strategy = str(self.config.fallback_poll_strategy or "disabled").strip().lower() or "disabled"
        if strategy in {"rotate", "sessions"}:
            target_chats = chat_names
        else:
            pinned = (
                str(self.config.fallback_poll_chat or "").strip()
                or str(self._pinned_chat_name or "").strip()
                or (chat_names[0] if chat_names else "")
            )
            target_chats = [pinned] if pinned else []
        for chat_name in target_chats:
            try:
                self._select_chat(chat_name)
                items = self._wx.GetAllMessage()
                normalized.extend(self._normalize_incremental_chat_messages(chat_name, items))
            except Exception as exc:  # pragma: no cover - depends on Windows env
                logger.warning("GetAllMessage failed for chat=%s error=%s", chat_name, exc)
        return normalized

    def _auto_expand_listen_targets(self, *, use_callback: bool) -> None:
        if not self.config.auto_expand_listen:
            return
        now = time.time()
        interval = max(float(self.config.auto_expand_scan_interval_seconds or 0), 1.0)
        if now - self._last_auto_expand_scan_at < interval:
            return
        self._last_auto_expand_scan_at = now
        for chat_name in self._discover_session_chat_names():
            if len(self._chat_cache) >= max(int(self.config.auto_expand_max_chats or 0), 1):
                break
            self._ensure_listen_chat(chat_name, use_callback=use_callback, source="auto")

    def _discover_session_chat_names(self) -> list[str]:
        if self._wx is None:
            return []
        candidates: list[str] = []
        seen: set[str] = set(self._chat_cache.keys())
        for api_name in ("GetSession", "GetSessionList", "GetSessionListNew", "GetRecentChat", "GetRecentChats", "GetAllChats"):
            func = getattr(self._wx, api_name, None)
            if not callable(func):
                continue
            try:
                result = func()
            except Exception as exc:  # pragma: no cover - depends on Windows env
                logger.debug("session discovery api failed | api=%s | error=%s", api_name, exc)
                continue
            for chat_name in self._extract_chat_names_from_result(result):
                if chat_name in seen:
                    continue
                seen.add(chat_name)
                candidates.append(chat_name)
        return candidates

    def _extract_chat_names_from_result(self, result: Any) -> list[str]:
        names: list[str] = []
        if isinstance(result, dict):
            descriptor = self._normalize_chat_info_dict(result)
            chat_name = str(descriptor.get("chat_name") or "").strip()
            if chat_name:
                return [chat_name]
            for value in result.values():
                names.extend(self._extract_chat_names_from_result(value))
            return names
        if isinstance(result, (list, tuple, set)):
            for item in result:
                names.extend(self._extract_chat_names_from_result(item))
            return names
        if isinstance(result, str):
            normalized = result.strip()
            return [normalized] if normalized else []
        if result is None:
            return []
        descriptor = self._extract_chat_descriptor(result)
        chat_name = str(descriptor.get("chat_name") or "").strip()
        return [chat_name] if chat_name else []

    def _normalize_incremental_chat_messages(self, chat_name: str, items: Any) -> list[NormalizedMessage]:
        if not isinstance(items, list):
            return []
        recent_signatures = self._chat_recent_signatures.setdefault(chat_name, collections.deque(maxlen=200))
        known_signatures = set(recent_signatures)
        normalized: list[NormalizedMessage] = []

        # GetAllMessage returns the full visible/current conversation history, so we only
        # emit the unseen tail while keeping a rolling signature window per chat.
        candidate_items = items[-80:] if len(items) > 80 else items
        for item in candidate_items:
            raw_signature = self._build_raw_item_signature(chat_name, item)
            if raw_signature in known_signatures:
                continue
            converted = self._normalize_single_message(chat_name, item)
            if converted is None:
                recent_signatures.append(raw_signature)
                known_signatures.add(raw_signature)
                continue
            if converted.event_key in self._last_message_fingerprints:
                recent_signatures.append(raw_signature)
                known_signatures.add(raw_signature)
                continue
            self._last_message_fingerprints.add(converted.event_key)
            recent_signatures.append(raw_signature)
            known_signatures.add(raw_signature)
            normalized.append(converted)
        return normalized

    def _normalize_single_message(
        self,
        chat_name: str,
        item: Any,
        *,
        chat_descriptor: dict[str, Any] | None = None,
    ) -> NormalizedMessage | None:
        if item is None:
            return None
        raw_type = type(item).__name__
        chat_descriptor = chat_descriptor or {}
        chat_info = self._merge_chat_context(chat_descriptor, self._extract_chat_info(item))
        sender_name = self._pick_attr(item, "sender", "Sender", "sender_name", "name")
        raw_attr = self._pick_attr(item, "raw", "Raw")
        content = self._pick_attr(item, "content", "Content", "text", "Text")
        if content in (None, "") and raw_attr not in (None, ""):
            content = raw_attr
        content = content or str(item).strip()
        content = str(content or "").strip()
        if not content:
            return None
        raw_timestamp = self._pick_attr(item, "time", "Time", "timestamp", "Timestamp") or ""
        normalized_timestamp, source_timestamp = normalize_provider_timestamp(raw_timestamp)
        message_type = self._infer_message_type(item, raw_type, content)
        conversation_title = (
            str(chat_info.get("chat_name") or "").strip()
            or chat_name
            or self._pick_attr(item, "chat_name", "ChatName")
            or "unknown"
        )
        chat_type = self._infer_chat_type(conversation_title, item, content, chat_info)
        sender_name, normalized_content = self._extract_group_sender(sender_name, content, chat_type)
        is_self = self._is_self_message(sender_name, item, chat_info)
        sender_id = f"wxauto:{sender_name}" if sender_name else None
        message_hash = self._pick_attr(item, "hash", "Hash")
        file_name = self._pick_attr(item, "filename", "file_name", "FileName", "name", "Name")
        event_key = self._build_event_key(
            conversation_title,
            sender_name,
            normalized_content,
            normalized_timestamp,
            message_type,
            message_hash=message_hash,
            file_name=str(file_name or "").strip() or None,
        )
        media_details = self._extract_media_details(item, message_type, event_key)
        if message_type in {"image", "file", "video"} and not normalized_content:
            normalized_content = f"[{message_type}]"
        normalized_message = NormalizedMessage(
            event_key=event_key,
            conversation_external_id=self._build_conversation_external_id(conversation_title, chat_type),
            conversation_title=conversation_title,
            chat_type=chat_type,
            sender_id=sender_id,
            sender_name=str(sender_name).strip() if sender_name else None,
            content=normalized_content,
            message_type=message_type,
            timestamp=normalized_timestamp,
            details={
                "provider_payload_type": raw_type,
                "provider_message_type": message_type,
                "raw_chat_name": conversation_title,
                "source_timestamp": source_timestamp,
                "inferred_chat_type": chat_type,
                "is_self": is_self,
                "message_attr": self._pick_attr(item, "attr", "Attr"),
                "message_hash": str(message_hash or "").strip() or None,
                "chat_info": chat_info,
                "chat_descriptor": chat_descriptor,
            }
            | media_details,
        )
        self._remember_observed_chat(conversation_title)
        if media_details.get("download_pending") and self.config.media_download_async:
            self._enqueue_media_download(item=item, message=normalized_message)
        return normalized_message

    def _remember_observed_chat(self, chat_name: str) -> None:
        normalized = str(chat_name or "").strip()
        if not normalized:
            return
        self._chat_cache.setdefault(normalized, normalized)

    def _pick_attr(self, item: Any, *names: str) -> Any:
        for name in names:
            if hasattr(item, name):
                value = getattr(item, name)
                if value not in (None, ""):
                    return value
            if isinstance(item, dict):
                value = item.get(name)
                if value not in (None, ""):
                    return value
        return None

    def _infer_message_type(self, item: Any, raw_type: str, content: str) -> str:
        explicit = self._pick_attr(item, "type", "Type", "msg_type", "MsgType")
        explicit_text = str(explicit or "").strip().lower()
        if explicit_text in {"text", "image", "file", "video", "voice", "quote", "system"}:
            return explicit_text
        lowered_type = raw_type.lower()
        if "image" in lowered_type:
            return "image"
        if "voice" in lowered_type or "audio" in lowered_type:
            return "voice"
        if "video" in lowered_type:
            return "video"
        if "file" in lowered_type:
            return "file"
        if content.startswith("[图片]"):
            return "image"
        if content.startswith("[文件]"):
            return "file"
        return "text"

    def _extract_chat_info(self, item: Any) -> dict[str, Any]:
        chat_info = getattr(item, "chat_info", None)
        if callable(chat_info):
            try:
                result = chat_info()
                if isinstance(result, dict):
                    return self._normalize_chat_info_dict(result)
                return {"value": safe_jsonable(result)}
            except Exception as exc:  # pragma: no cover - depends on wxauto env
                logger.debug("chat_info() failed: %s", exc)
        return {}

    def _extract_chat_descriptor(self, chat_key: Any) -> dict[str, Any]:
        if isinstance(chat_key, dict):
            return self._normalize_chat_info_dict(chat_key)
        if isinstance(chat_key, str):
            return {"chat_name": chat_key.strip()}
        descriptor: dict[str, Any] = {}
        chat_name = self._pick_attr(chat_key, "name", "Name", "who", "Who", "chat_name", "ChatName", "title", "Title")
        chat_type = self._pick_attr(chat_key, "chat_type", "ChatType", "type", "Type")
        if chat_name not in (None, ""):
            descriptor["chat_name"] = str(chat_name).strip()
        if chat_type not in (None, ""):
            descriptor["chat_type"] = str(chat_type).strip().lower()
        if descriptor:
            return descriptor
        try:
            raw_dict = vars(chat_key)
        except Exception:
            raw_dict = None
        if isinstance(raw_dict, dict):
            descriptor = self._normalize_chat_info_dict(raw_dict)
        if "chat_name" not in descriptor:
            try:
                fallback_name = str(chat_key).strip()
            except Exception:
                fallback_name = ""
            if fallback_name:
                descriptor["chat_name"] = fallback_name
        return descriptor

    def _normalize_chat_info_dict(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        normalized = {str(key).strip().lower(): safe_jsonable(value) for key, value in raw.items()}
        chat_name = (
            normalized.get("chat_name")
            or normalized.get("name")
            or normalized.get("who")
            or normalized.get("chat")
            or normalized.get("title")
        )
        chat_type = normalized.get("chat_type") or normalized.get("type")
        descriptor: dict[str, Any] = dict(normalized)
        if chat_name not in (None, ""):
            descriptor["chat_name"] = str(chat_name).strip()
        if chat_type not in (None, ""):
            descriptor["chat_type"] = str(chat_type).strip().lower()
        return descriptor

    def _merge_chat_context(self, *parts: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for part in parts:
            if not isinstance(part, dict):
                continue
            for key, value in part.items():
                if value in (None, ""):
                    continue
                merged[str(key)] = value
        return merged

    def _looks_like_group_name(self, value: str) -> bool:
        return any(marker in str(value or "") for marker in ("群", "chatroom"))

    def _build_conversation_external_id(self, chat_name: str, chat_type: str) -> str:
        mapped_external_id = self._resolve_external_id_from_alias(chat_name)
        if mapped_external_id:
            return mapped_external_id
        prefix = "group" if chat_type == "group" else "user"
        return f"{prefix}:{chat_name}"

    def _infer_chat_type(self, chat_name: str, item: Any, content: str, chat_info: dict[str, Any]) -> str:
        mapped_external_id = self._resolve_external_id_from_alias(chat_name)
        if mapped_external_id.startswith("group:"):
            return "group"
        if mapped_external_id.startswith("user:"):
            return "direct"

        chat_info_type = str(chat_info.get("chat_type") or "").strip().lower()
        if chat_info_type in {"group", "chatroom"}:
            return "group"
        if chat_info_type in {"friend", "direct", "service", "official", "user"}:
            return "direct"

        if bool(chat_info.get("is_group")):
            return "group"
        if str(chat_info.get("wxid") or "").strip().endswith("@chatroom"):
            return "group"
        if str(chat_info.get("who") or "").strip().endswith("@chatroom"):
            return "group"

        explicit_chat_type = self._pick_attr(item, "chat_type", "ChatType", "type_name", "TypeName")
        explicit_text = str(explicit_chat_type or "").strip().lower()
        if explicit_text in {"group", "chatroom"}:
            return "group"
        if explicit_text in {"direct", "friend", "user"}:
            return "direct"

        if "@chatroom" in chat_name or self._looks_like_group_name(chat_name):
            return "group"
        if self._looks_like_group_sender_content(content):
            return "group"
        return "direct"

    def _resolve_external_id_from_alias(self, chat_name: str) -> str:
        normalized_chat_name = str(chat_name or "").strip()
        if not normalized_chat_name:
            return ""
        for external_id, alias_name in self.config.chat_alias_map.items():
            if str(alias_name or "").strip() == normalized_chat_name:
                return str(external_id or "").strip()
        return ""

    def _looks_like_group_sender_content(self, content: str) -> bool:
        normalized = str(content or "").strip()
        if "\n" not in normalized:
            return False
        first_line = normalized.split("\n", 1)[0].strip()
        if not first_line:
            return False
        return first_line.endswith(":") or first_line.endswith("：")

    def _extract_group_sender(self, sender_name: Any, content: str, chat_type: str) -> tuple[str | None, str]:
        normalized_sender = str(sender_name or "").strip() or None
        normalized_content = str(content or "").strip()
        if chat_type != "group":
            return normalized_sender, normalized_content
        if "\n" not in normalized_content:
            return normalized_sender, normalized_content
        first_line, remainder = normalized_content.split("\n", 1)
        candidate_sender = first_line.strip().rstrip(":").rstrip("：").strip()
        if candidate_sender and not normalized_sender:
            normalized_sender = candidate_sender
            normalized_content = remainder.strip()
        return normalized_sender, normalized_content

    def _is_self_message(self, sender_name: Any, item: Any, chat_info: dict[str, Any]) -> bool:
        configured_self_name = str(self.config.self_name or "").strip()
        normalized_sender = str(sender_name or "").strip()
        if configured_self_name and normalized_sender and configured_self_name == normalized_sender:
            return True
        is_self = self._pick_attr(item, "is_self", "IsSelf", "self", "Self")
        if isinstance(is_self, bool):
            return is_self
        if str(chat_info.get("sender_type") or "").strip().lower() == "self":
            return True
        return False

    def _build_event_key(
        self,
        chat_name: str,
        sender_name: str | None,
        content: str,
        timestamp: str,
        message_type: str,
        *,
        message_hash: Any = None,
        file_name: str | None = None,
    ) -> str:
        normalized_hash = str(message_hash or "").strip()
        normalized_message_type = str(message_type or "").strip()
        normalized_file_name = str(file_name or "").strip()
        # File/media hashes from wxauto can remain stable across repeated sends of the
        # same asset, so media event keys need extra entropy beyond message_hash.
        if normalized_hash and normalized_message_type not in {"file", "image", "video", "voice"}:
            return hashlib.sha1(f"{chat_name.strip()}|{normalized_hash}".encode("utf-8")).hexdigest()
        source_parts = [
            chat_name.strip(),
            str(sender_name or "").strip(),
            content.strip(),
            str(timestamp).strip(),
            normalized_message_type,
        ]
        if normalized_hash:
            source_parts.append(normalized_hash)
        if normalized_file_name:
            source_parts.append(normalized_file_name)
        source = "|".join(source_parts)
        return hashlib.sha1(source.encode("utf-8")).hexdigest()

    def _build_raw_item_signature(self, chat_name: str, item: Any) -> str:
        message_hash = self._pick_attr(item, "hash", "Hash")
        normalized_hash = str(message_hash or "").strip()
        if normalized_hash:
            return hashlib.sha1(f"{str(chat_name or '').strip()}|{normalized_hash}".encode("utf-8")).hexdigest()
        raw_type = type(item).__name__
        sender_name = self._pick_attr(item, "sender", "Sender", "sender_name", "name")
        raw_attr = self._pick_attr(item, "raw", "Raw")
        content = self._pick_attr(item, "content", "Content", "text", "Text")
        if content in (None, "") and raw_attr not in (None, ""):
            content = raw_attr
        timestamp = self._pick_attr(item, "time", "Time", "timestamp", "Timestamp") or ""
        file_name = self._pick_attr(item, "filename", "file_name", "FileName", "name", "Name") or ""
        stable_source = "|".join(
            [
                str(chat_name or "").strip(),
                str(sender_name or "").strip(),
                str(content or "").strip(),
                str(file_name or "").strip(),
                str(timestamp or "").strip(),
                raw_type,
            ]
        )
        return hashlib.sha1(stable_source.encode("utf-8")).hexdigest()

    def _extract_media_details(self, item: Any, message_type: str, event_key: str) -> dict[str, Any]:
        details: dict[str, Any] = {}
        file_name = self._pick_attr(item, "filename", "file_name", "FileName", "name", "Name")
        file_path = self._pick_attr(item, "path", "file_path", "Path")
        if file_name:
            details["file_name"] = str(file_name).strip()
        if file_path:
            details["file_path"] = str(file_path).strip()
        if message_type not in {"image", "file", "video"}:
            return details
        if not self.config.download_media:
            details["download_skipped"] = True
            return details
        if file_path:
            return details
        download = getattr(item, "download", None)
        if not callable(download):
            return details
        if self.config.media_download_async:
            details["download_pending"] = True
            details["download_async"] = True
            return details
        return self._download_media_details(item, message_type=message_type, event_key=event_key, initial_details=details)

    def _download_media_details(
        self,
        item: Any,
        *,
        message_type: str,
        event_key: str,
        initial_details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        details: dict[str, Any] = dict(initial_details or {})
        file_name = self._pick_attr(item, "filename", "file_name", "FileName", "name", "Name")
        file_path = self._pick_attr(item, "path", "file_path", "Path")
        if file_name and "file_name" not in details:
            details["file_name"] = str(file_name).strip()
        if file_path:
            details["file_path"] = str(file_path).strip()
            return details
        download = getattr(item, "download", None)
        if not callable(download):
            return details
        retry_count = max(int(self.config.media_download_retry_count or 0), 0)
        retry_delay_seconds = max(float(self.config.media_download_retry_delay_seconds or 0), 0.0)
        initial_delay_seconds = max(float(self.config.media_download_initial_delay_seconds or 0), 0.0)
        try:
            download_dir = Path(self.config.media_download_dir).expanduser().resolve() / message_type
            download_dir.mkdir(parents=True, exist_ok=True)
            attempts: list[dict[str, Any]] = []
            max_attempts = retry_count + 1
            for attempt in range(1, max_attempts + 1):
                if attempt == 1 and initial_delay_seconds > 0:
                    time.sleep(initial_delay_seconds)
                elif attempt > 1 and retry_delay_seconds > 0:
                    time.sleep(retry_delay_seconds)

                dir_snapshot_before = _list_download_dir_files(download_dir)
                downloaded = self._invoke_media_download(download, download_dir)
                failed, failed_reason = _looks_like_download_failure(downloaded)
                resolved_path = None if failed else self._resolve_downloaded_path(
                    downloaded,
                    base_dir=download_dir,
                    file_name=str(details.get("file_name") or "").strip() or None,
                    dir_snapshot_before=dir_snapshot_before,
                    item=item,
                )
                attempt_record = {
                    "attempt": attempt,
                    "result": safe_jsonable(downloaded),
                    "failed": failed,
                    "reason": failed_reason,
                    "resolved_path": str(resolved_path) if resolved_path else None,
                }
                attempts.append(attempt_record)
                logger.info(
                    "media download attempt | event=%s | type=%s | attempt=%s/%s | failed=%s | reason=%s | result=%s",
                    event_key,
                    message_type,
                    attempt,
                    max_attempts,
                    failed,
                    failed_reason or "",
                    safe_jsonable(downloaded),
                )
                if resolved_path is not None:
                    details["download_path"] = str(resolved_path)
                    details.setdefault("file_name", resolved_path.name)
                    details["download_attempts"] = attempts
                    return details
                if not failed:
                    details["download_result"] = safe_jsonable(downloaded)
                    details["download_attempts"] = attempts
                    return details

            final_result = attempts[-1]["result"] if attempts else None
            details["download_result"] = final_result
            details["download_error"] = str(attempts[-1].get("reason") or "media download failed")
            details["download_attempts"] = attempts
        except Exception as exc:
            details["download_error"] = str(exc)
            logger.warning("media download failed event=%s type=%s error=%s", event_key, message_type, exc)
        return details

    def _invoke_media_download(self, download: Callable[..., Any], download_dir: Path) -> Any:
        try:
            parameters = inspect.signature(download).parameters
        except (TypeError, ValueError):
            parameters = {}

        preferred_kwargs = [
            ("dir_path", str(download_dir)),
            ("savepath", str(download_dir)),
            ("save_path", str(download_dir)),
            ("path", str(download_dir)),
            ("dir", str(download_dir)),
            ("dirname", str(download_dir)),
            ("folder", str(download_dir)),
        ]
        for key, value in preferred_kwargs:
            if key not in parameters:
                continue
            try:
                return download(**{key: value})
            except TypeError:
                continue

        if len(parameters) == 1:
            try:
                return download(str(download_dir))
            except TypeError:
                pass

        return download()

    def _resolve_downloaded_path(
        self,
        downloaded: Any,
        *,
        base_dir: Path,
        file_name: str | None,
        dir_snapshot_before: dict[str, float],
        item: Any,
    ) -> Path | None:
        direct_candidates: list[Any] = [downloaded]
        if isinstance(downloaded, dict):
            direct_candidates.extend(
                downloaded.get(key)
                for key in ("download_path", "file_path", "path", "saved_path", "save_path", "target_path", "result")
            )
        direct_candidates.append(self._pick_attr(item, "path", "file_path", "Path"))
        for candidate in direct_candidates:
            resolved = _coerce_existing_path(candidate, base_dir=base_dir)
            if resolved is not None:
                return resolved

        if file_name:
            resolved = _coerce_existing_path(file_name, base_dir=base_dir)
            if resolved is not None:
                return resolved

        dir_snapshot_after = _list_download_dir_files(base_dir)
        new_paths = [Path(path) for path in dir_snapshot_after if path not in dir_snapshot_before]
        if file_name:
            matching_name = [path for path in new_paths if path.name == file_name]
            if len(matching_name) == 1:
                return matching_name[0].resolve()
        if len(new_paths) == 1:
            return new_paths[0].resolve()
        if new_paths:
            return max(new_paths, key=lambda path: dir_snapshot_after.get(str(path), 0.0)).resolve()

        if file_name:
            candidates = [path for path in base_dir.glob("*") if path.is_file() and path.name == file_name]
            if candidates:
                return max(candidates, key=lambda path: path.stat().st_mtime).resolve()
        return None


class ProviderRuntime:
    def __init__(self, config: ProviderConfig) -> None:
        self.config = config
        self.wxauto = WxAutoClient(config)
        self.wxauto.set_media_download_callback(self._handle_media_download_completed)
        self.listen_status = ListenStatus(
            running=False,
            receive_mode="listener_only" if not self.wxauto.fallback_poll_enabled() else "listener_with_fallback",
            fallback_poll_enabled=self.wxauto.fallback_poll_enabled(),
        )
        self._listen_thread: threading.Thread | None = None
        self._listen_stop = threading.Event()
        self._callback_lock = threading.Lock()
        self._recent_messages: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=max(int(config.debug_recent_message_limit or 50), 1)
        )

    def start_listener(self) -> None:
        if self._listen_thread and self._listen_thread.is_alive():
            return
        self._listen_stop.clear()
        self._listen_thread = threading.Thread(target=self._listen_loop, name="wxauto-listener", daemon=True)
        self._listen_thread.start()

    def stop_listener(self) -> None:
        self._listen_stop.set()
        if self._listen_thread and self._listen_thread.is_alive():
            self._listen_thread.join(timeout=5)
        self.wxauto.stop_background_workers()

    def send_message(self, payload: SendMessageRequest) -> dict[str, Any]:
        if self.config.provider_token:
            logger.info("handling send-message request for %s", payload.receiver_id or payload.conversation_title)
        target_name = (
            str(payload.receiver_id or "").strip()
            or str(payload.conversation_external_id or "").strip()
            or str(payload.conversation_title or "").strip()
        )
        if ":" in target_name:
            target_name = self.config.chat_alias_map.get(target_name, target_name.split(":", 1)[1])
        outbound_message_type = str(payload.message_type or "text").strip().lower()
        outbound_content = self._resolve_outbound_content(payload, message_type=outbound_message_type)
        result = self.wxauto.send_message(
            target_name,
            message_type=outbound_message_type,
            content=outbound_content,
        )
        return result

    def health_payload(self) -> dict[str, Any]:
        status = "online"
        ok = True
        summary = "provider listening"
        if self.config.listen_chats and not self.listen_status.running:
            status = "degraded"
            ok = False
            summary = self.listen_status.last_error or "listener not running"
        elif not self.wxauto.ready:
            status = "idle"
            summary = "wxauto not initialized"
        return {
            "ok": ok,
            "status": status,
            "summary": summary,
            "timestamp": utc_now_iso(),
            "listen_status": self.listen_status.model_dump(),
            "wxauto_ready": self.wxauto.ready,
            "config": {
                "listen_chats": self.config.listen_chats,
                "auto_expand_listen": self.config.auto_expand_listen,
                "auto_expand_scan_interval_seconds": self.config.auto_expand_scan_interval_seconds,
                "auto_expand_max_chats": self.config.auto_expand_max_chats,
                "callback_url": self.config.callback_url,
                "fallback_poll_strategy": self.config.fallback_poll_strategy,
            },
            "capabilities": {
                "listener_api": self.wxauto.listener_api_name(),
                "supported_receive_apis": self.wxauto.supported_receive_apis() if self.wxauto.ready else [],
            },
        }

    def _listen_loop(self) -> None:
        self.listen_status.seed_chats = self.wxauto.list_seed_chats()
        self.listen_status.listened_chats = self.wxauto.list_listened_chats()
        while not self._listen_stop.is_set():
            try:
                if not self.listen_status.running:
                    self.listen_status.startup_attempts += 1
                    self.wxauto.start_listening()
                    self.listen_status.running = True
                    self.listen_status.wxauto_ready = True
                    self.listen_status.listener_api = self.wxauto.listener_api_name()
                    self.listen_status.fallback_poll_enabled = self.wxauto.fallback_poll_enabled()
                    self.listen_status.supported_receive_apis = self.wxauto.supported_receive_apis()
                    self.listen_status.receive_mode = (
                        "listener_only" if not self.listen_status.fallback_poll_enabled else "listener_with_fallback"
                    )
                    self.listen_status.consecutive_failures = 0
                    self.listen_status.last_error = None
                    logger.info("listener startup completed")
                self.listen_status.seed_chats = self.wxauto.list_seed_chats()
                self.listen_status.listened_chats = self.wxauto.list_listened_chats()
                self.listen_status.auto_discovered_chats = self.wxauto.list_auto_discovered_chats()
                self.listen_status.listen_failures = self.wxauto.listen_failures()
                messages = self.wxauto.poll_messages()
                self.listen_status.last_poll_at = utc_now_iso()
                self.listen_status.last_receive_source = self.wxauto.last_receive_source
                self.listen_status.pending_messages = len(messages)
                if messages:
                    logger.info(
                        "provider captured messages | count=%s | source=%s",
                        len(messages),
                        self.wxauto.last_receive_source,
                    )
                for message in messages:
                    logger.info("provider normalized message | %s", json.dumps(build_message_log_payload(message), ensure_ascii=False))
                    self._record_recent_message(message)
                    self._post_callback(message)
                time.sleep(max(float(self.config.poll_interval_seconds or DEFAULT_POLL_INTERVAL_SECONDS), 0.5))
                continue
            except WxAutoListenerUnavailableError as exc:
                self.listen_status.running = False
                self.listen_status.wxauto_ready = self.wxauto.ready
                self.listen_status.listener_api = self.wxauto.listener_api_name()
                self.listen_status.supported_receive_apis = self.wxauto.supported_receive_apis()
                self.listen_status.last_receive_source = self.wxauto.last_receive_source
                self.listen_status.last_error = str(exc)
                self.listen_status.last_error_at = utc_now_iso()
                self.listen_status.consecutive_failures += 1
                self.listen_status.receive_mode = "listener_unavailable"
                logger.warning("listener receive api unavailable: %s", exc)
                time.sleep(max(float(self.config.startup_retry_seconds or 5.0), 2.0))
                continue
            except Exception as exc:
                self.listen_status.running = False
                self.listen_status.wxauto_ready = self.wxauto.ready
                self.listen_status.supported_receive_apis = self.wxauto.supported_receive_apis() if self.wxauto.ready else []
                self.listen_status.last_error = str(exc)
                self.listen_status.last_error_at = utc_now_iso()
                self.listen_status.consecutive_failures += 1
                logger.exception("listener loop failed")
                if self.config.fail_on_missing_wxauto:
                    raise
                retry_seconds = (
                    float(self.config.startup_retry_seconds or 5.0)
                    if "wxauto unavailable" in str(exc).lower() or "register listen chat failed" in str(exc).lower()
                    else float(self.config.poll_error_retry_seconds or 3.0)
                )
                time.sleep(max(retry_seconds, 0.5))
        self.listen_status.running = False

    def _post_callback(self, message: NormalizedMessage) -> None:
        if not self.config.callback_url:
            logger.info("callback_url not configured, skipping inbound callback: %s", message.event_key)
            return
        payload = message.to_webhook_payload()
        self._post_callback_payload(payload, event_key=message.event_key, log_label="inbound callback")

    def _handle_media_download_completed(self, message: NormalizedMessage, media_details: dict[str, Any]) -> None:
        if not self.config.callback_url:
            return
        payload = {
            "event_key": f"{message.event_key}:attachment",
            "event_type": "message_attachment_ready",
            "conversation": {
                "external_id": message.conversation_external_id,
                "title": message.conversation_title,
                "chat_type": message.chat_type,
            },
            "message": {
                "content": "",
                "sender_id": message.sender_id,
                "sender_name": message.sender_name,
                "message_type": message.message_type,
                "external_message_id": None,
            },
            "details": {
                "timestamp": utc_now_iso(),
                "attachment_event_for": message.event_key,
                "attachment_message_type": message.message_type,
                "attachment_ready": bool(media_details.get("download_path") or media_details.get("file_path")),
                "attachment_details": safe_jsonable(media_details),
            },
        }
        self._post_callback_payload(payload, event_key=str(payload["event_key"]), log_label="attachment callback")

    def _post_callback_payload(self, payload: dict[str, Any], *, event_key: str, log_label: str) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": "wechat-winauto-provider/1.0"}
        timestamp = str(int(time.time()))
        if self.config.callback_secret:
            headers["X-Timestamp"] = timestamp
            headers["X-Signature"] = f"sha256={sign_payload(self.config.callback_secret, body, timestamp)}"
        req = urlrequest.Request(self.config.callback_url, data=body, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)

        with self._callback_lock:
            try:
                with urlrequest.urlopen(req, timeout=self.config.callback_timeout_seconds) as response:
                    response.read()
                    self.listen_status.last_callback_at = utc_now_iso()
                    self.listen_status.callback_success_count += 1
                    self.listen_status.delivered_message_count += 1
                    self.listen_status.last_message_at = utc_now_iso()
                    message_preview = str((payload.get("message") or {}).get("content") or "")
                    self.listen_status.last_message_preview = message_preview[:120]
                    logger.info("%s succeeded: %s status=%s", log_label, event_key, getattr(response, "status", 200))
            except HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                self.listen_status.last_error = f"callback_http_error:{exc.code}:{body_text}"
                self.listen_status.last_error_at = utc_now_iso()
                self.listen_status.callback_failure_count += 1
                logger.error("%s http error: %s", log_label, self.listen_status.last_error)
            except URLError as exc:
                self.listen_status.last_error = f"callback_network_error:{exc.reason or exc}"
                self.listen_status.last_error_at = utc_now_iso()
                self.listen_status.callback_failure_count += 1
                logger.error("%s network error: %s", log_label, self.listen_status.last_error)

    def _record_recent_message(self, message: NormalizedMessage) -> None:
        payload = message.to_webhook_payload()
        event_record = {
            "captured_at": utc_now_iso(),
            "event_key": message.event_key,
            "conversation_external_id": message.conversation_external_id,
            "conversation_title": message.conversation_title,
            "message_type": message.message_type,
            "sender_name": message.sender_name,
            "content_preview": message.content[:200],
            "payload": payload,
        }
        self._recent_messages.append(event_record)
        if not self.config.debug_dump_raw_messages:
            return
        dump_dir = Path(self.config.debug_dump_dir).expanduser().resolve()
        dump_dir.mkdir(parents=True, exist_ok=True)
        dump_path = dump_dir / f"{message.event_key}.json"
        dump_path.write_text(json.dumps(safe_jsonable(event_record), ensure_ascii=False, indent=2), encoding="utf-8")

    def recent_messages_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "count": len(self._recent_messages),
            "items": list(self._recent_messages),
        }

    def _resolve_outbound_content(self, payload: SendMessageRequest, *, message_type: str) -> str:
        if message_type == "text":
            content = str(payload.content or "").strip()
            if not content:
                raise ValueError("empty content")
            return content
        if message_type not in {"image", "file"}:
            raise ValueError(f"unsupported message_type: {message_type}")

        raw_payload = payload.payload if isinstance(payload.payload, dict) else {}
        candidate_path = self._first_non_empty(
            raw_payload,
            "file_path",
            "image_path",
            "path",
            "local_path",
        )
        if candidate_path:
            resolved = Path(candidate_path).expanduser()
            if not resolved.exists():
                raise ValueError(f"file not found: {resolved}")
            return str(resolved.resolve())

        candidate_url = self._first_non_empty(
            raw_payload,
            "file_url",
            "image_url",
            "download_url",
            "url",
        )
        if candidate_url:
            return str(self._download_remote_asset(candidate_url, raw_payload, message_type=message_type))

        candidate_base64 = self._first_non_empty(
            raw_payload,
            "base64",
            "image_base64",
            "file_base64",
        )
        if candidate_base64:
            return str(self._write_base64_asset(candidate_base64, raw_payload, message_type=message_type))

        content = str(payload.content or "").strip()
        if content and Path(content).exists():
            return str(Path(content).resolve())
        raise ValueError(f"missing {message_type} payload")

    def _download_remote_asset(self, url: str, raw_payload: dict[str, Any], *, message_type: str) -> Path:
        parsed = urlparse.urlparse(url)
        suffix = Path(parsed.path).suffix or self._guess_suffix(raw_payload, message_type=message_type)
        file_name = self._first_non_empty(raw_payload, "file_name", "name") or f"{message_type}-{int(time.time() * 1000)}{suffix}"
        target_dir = OUTBOUND_CACHE_ROOT / message_type
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / self._safe_file_name(file_name)
        req = urlrequest.Request(url, method="GET")
        with urlrequest.urlopen(req, timeout=self.config.callback_timeout_seconds) as response:
            target_path.write_bytes(response.read())
        return target_path.resolve()

    def _write_base64_asset(self, raw_value: str, raw_payload: dict[str, Any], *, message_type: str) -> Path:
        normalized = str(raw_value or "").strip()
        mime_type = ""
        if normalized.startswith("data:") and "," in normalized:
            header, normalized = normalized.split(",", 1)
            mime_type = header[5:].split(";", 1)[0]
        binary = base64.b64decode(normalized)
        suffix = self._guess_suffix(raw_payload, message_type=message_type, mime_type=mime_type)
        file_name = self._first_non_empty(raw_payload, "file_name", "name") or f"{message_type}-{int(time.time() * 1000)}{suffix}"
        target_dir = OUTBOUND_CACHE_ROOT / message_type
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / self._safe_file_name(file_name)
        target_path.write_bytes(binary)
        return target_path.resolve()

    def _guess_suffix(self, raw_payload: dict[str, Any], *, message_type: str, mime_type: str = "") -> str:
        file_name = self._first_non_empty(raw_payload, "file_name", "name")
        if file_name and Path(file_name).suffix:
            return Path(file_name).suffix
        if mime_type:
            guessed = mimetypes.guess_extension(mime_type)
            if guessed:
                return guessed
        return ".jpg" if message_type == "image" else ".bin"

    def _safe_file_name(self, value: str) -> str:
        invalid_chars = '<>:"/\\|?*'
        normalized = "".join("_" if char in invalid_chars else char for char in str(value or "").strip())
        return normalized[:180] or f"file-{int(time.time() * 1000)}"

    def _first_non_empty(self, payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value in (None, ""):
                continue
            normalized = str(value).strip()
            if normalized:
                return normalized
        return None


config = load_config()
runtime = ProviderRuntime(config)
app = FastAPI(title="wechat_winauto_provider", version="0.1.0")


@app.on_event("startup")
async def startup_event() -> None:
    runtime.start_listener()


@app.on_event("shutdown")
async def shutdown_event() -> None:
    runtime.stop_listener()


@app.get("/health")
async def health() -> dict[str, Any]:
    return runtime.health_payload()


@app.get("/listen-status")
async def listen_status() -> dict[str, Any]:
    return runtime.listen_status.model_dump()


@app.get("/debug/recent-messages")
async def debug_recent_messages(request: Request) -> dict[str, Any]:
    assert_provider_authorized(request)
    return runtime.recent_messages_payload()


@app.post("/send-message")
async def send_message(payload: SendMessageRequest, request: Request) -> dict[str, Any]:
    assert_provider_authorized(request)
    try:
        return runtime.send_message(payload)
    except WxAutoUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("send-message failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/reload")
async def reload_config(request: Request) -> dict[str, Any]:
    assert_provider_authorized(request)
    global config, runtime
    runtime.stop_listener()
    config = load_config()
    runtime = ProviderRuntime(config)
    runtime.start_listener()
    return {"ok": True, "status": "reloaded", "timestamp": utc_now_iso()}
