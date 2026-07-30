from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone

from ..agent.current_chat import CurrentChatCollector
from ..agent.navigator import Navigator
from ..agent.sender import Sender
from ..agent.session_list import SessionListCollector
from ..agent.watcher import SessionListWatcher
from ..agent.window_finder import WindowFinder
from ..state.event_store import EventStore
from ..state.message_cursor_store import MessageCursorStore
from ..state.preview_store import PreviewStore
from ..state.runtime_state import RuntimeState
from ..state.snapshot_store import SnapshotStore
from ..state.watcher_state_store import WatcherStateStore
from .config import settings
from .models import MessageCursorChatStateModel, MessageCursorStateModel, ProviderEventModel, SnapshotModel
from .schemas import (
    FetchFirstUnreadIncrementalRequest,
    SendFileRequest,
    SendTextRequest,
    SwitchFirstUnreadRequest,
    SwitchSessionRequest,
    WatcherControlResponse,
)

logger = logging.getLogger(__name__)


class ProviderRuntime:
    def __init__(self) -> None:
        self.window_finder = WindowFinder()
        self.session_collector = SessionListCollector(self.window_finder)
        self.current_chat_collector = CurrentChatCollector(self.window_finder)
        self.navigator = Navigator(self.window_finder)
        self.sender = Sender(self.window_finder, self.navigator)
        self.snapshot_store = SnapshotStore(settings.state_dir)
        self.event_store = EventStore(settings.state_dir)
        self.preview_store = PreviewStore(settings.state_dir)
        self.watcher_state_store = WatcherStateStore(settings.state_dir)
        self.message_cursor_store = MessageCursorStore(settings.state_dir)
        self.runtime_state = RuntimeState()
        self.watcher = SessionListWatcher(
            session_collector=self.session_collector,
            event_store=self.event_store,
            preview_store=self.preview_store,
            runtime_state=self.runtime_state,
            watcher_state_store=self.watcher_state_store,
            poll_interval_seconds=settings.poll_interval_seconds,
        )

    def health_report(self) -> dict:
        connected = self.window_finder.is_window_available()
        return {
            "ok": connected,
            "provider": settings.provider_name,
            "status": "online" if connected else "degraded",
            "summary": "微信窗口可访问" if connected else "尚未连接到微信窗口",
            "details": {
                "window_title": settings.main_window_title,
                "window_class_name": settings.main_window_class_name,
                "watcher_running": self.runtime_state.watcher_running,
                "last_window_error": self.window_finder.get_last_error(),
            },
        }

    def runtime_report(self) -> dict:
        return {
            "ok": True,
            "provider": settings.provider_name,
            "runtime": self.runtime_state.as_dict(),
            "watcher": self.watcher_status(),
        }

    def collect_snapshot(self) -> dict:
        sessions, session_error = self._safe_collect_sessions()
        conversation, conversation_error = self._safe_get_conversation()
        messages, message_error = self._safe_get_messages()
        snapshot = SnapshotModel(
            ok=not any((session_error, conversation_error, message_error)),
            provider=settings.provider_name,
            conversation=conversation,
            capabilities={
                "session_list_detected": bool(sessions),
                "message_list_detected": bool(messages),
                "input_box_detected": False,
            },
            chats=sessions,
            messages=messages,
            summary={
                "chat_count": len(sessions),
                "message_count": len(messages),
                "has_current_chat": conversation is not None,
            },
            meta={
                "window_available": self.window_finder.is_window_available(),
                "watcher_running": self.runtime_state.watcher_running,
                "last_window_error": self.window_finder.get_last_error(),
                "session_error": session_error,
                "conversation_error": conversation_error,
                "message_error": message_error,
            },
        )
        payload = snapshot.model_dump(mode="json")
        self.snapshot_store.save_snapshot(payload)
        self.preview_store.save_sessions(sessions)
        self.runtime_state.last_snapshot_at = datetime.now(timezone.utc)
        return payload

    def list_sessions(self):
        sessions, _ = self._safe_collect_sessions()
        return [item.model_dump(mode="json") for item in sessions]

    def list_unread_sessions(self, *, chat_type_hint: str | None = None) -> list[dict]:
        sessions, _ = self._safe_collect_sessions()
        filtered: list[dict] = []
        normalized_chat_type = str(chat_type_hint or "").strip().lower() or None
        for item in sessions:
            if not item.has_new:
                continue
            if normalized_chat_type and str(item.chat_type_hint or "").strip().lower() != normalized_chat_type:
                continue
            filtered.append(item.model_dump(mode="json"))
        return filtered

    def get_current_chat(self):
        conversation, _ = self._safe_get_conversation()
        return conversation.model_dump(mode="json") if conversation else None

    def get_current_messages(self):
        messages, _ = self._safe_get_messages()
        return [item.model_dump(mode="json") for item in messages]

    def debug_message_list_tree(self, *, max_depth: int = 4, max_children: int = 20) -> dict:
        try:
            return self.current_chat_collector.debug_message_list_tree(max_depth=max_depth, max_children=max_children)
        except Exception as exc:
            logger.warning("message list tree dump failed", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def debug_message_page_descendants(self, *, limit: int = 200) -> dict:
        try:
            return self.current_chat_collector.debug_message_page_descendants(limit=limit)
        except Exception as exc:
            logger.warning("message page descendants dump failed", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def debug_message_contexts(self, *, limit: int = 20) -> dict:
        try:
            return self.current_chat_collector.debug_message_contexts(limit=limit)
        except Exception as exc:
            logger.warning("message contexts dump failed", exc_info=True)
            return {"ok": False, "error": str(exc)}

    def get_current_chat_incremental(self, *, mark_as_seen: bool = True) -> dict:
        conversation, conversation_error = self._safe_get_conversation()
        messages, message_error = self._safe_get_messages()
        if conversation is None:
            return {
                "ok": False,
                "conversation": None,
                "messages": [],
                "new_messages": [],
                "summary": {"message_count": 0, "new_message_count": 0},
                "meta": {
                    "conversation_error": conversation_error,
                    "message_error": message_error,
                    "mark_as_seen": mark_as_seen,
                },
            }

        state = self.message_cursor_store.load_state()
        chat_state = state.chats.get(conversation.conversation_id) or MessageCursorChatStateModel()
        message_payloads = [item.model_dump(mode="json") for item in messages]
        current_fingerprints = [self._message_fingerprint(item) for item in message_payloads]
        overlap = self._suffix_overlap_length(chat_state.seen_fingerprints, current_fingerprints)
        new_messages = message_payloads[overlap:]

        if mark_as_seen:
            max_items = max(int(settings.message_cursor_max_ids_per_chat), 1)
            updated_seen_ids = [payload["message_id"] for payload in message_payloads][-max_items:]
            updated_seen_fingerprints = current_fingerprints[-max_items:]
            state.chats[conversation.conversation_id] = MessageCursorChatStateModel(
                seen_message_ids=updated_seen_ids,
                seen_fingerprints=updated_seen_fingerprints,
            )
            state = MessageCursorStateModel(chats=state.chats)
            self.message_cursor_store.save_state(state)

        return {
            "ok": not any((conversation_error, message_error)),
            "conversation": conversation.model_dump(mode="json"),
            "messages": message_payloads,
            "new_messages": new_messages,
            "summary": {"message_count": len(message_payloads), "new_message_count": len(new_messages)},
            "meta": {
                "conversation_error": conversation_error,
                "message_error": message_error,
                "mark_as_seen": mark_as_seen,
            },
        }

    def _message_fingerprint(self, payload: dict) -> str:
        raw = "|".join(
            [
                str(payload.get("sender_name") or ""),
                str(payload.get("message_type") or ""),
                str(payload.get("direction") or ""),
                str(payload.get("content") or ""),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _suffix_overlap_length(self, previous: list[str], current: list[str]) -> int:
        if not previous or not current:
            return 0
        max_overlap = min(len(previous), len(current))
        for size in range(max_overlap, 0, -1):
            if previous[-size:] == current[:size]:
                return size
        return 0

    def watcher_status(self) -> dict:
        return {
            "ok": True,
            "running": self.runtime_state.watcher_running,
            "last_started_at": self.runtime_state.iso_or_none(self.runtime_state.watcher_started_at),
            "last_stopped_at": self.runtime_state.iso_or_none(self.runtime_state.watcher_stopped_at),
            "last_poll_at": self.runtime_state.iso_or_none(self.runtime_state.last_watcher_poll_at),
            "poll_interval_seconds": settings.poll_interval_seconds,
            "recent_event_count": len(self.event_store.recent(limit=20)),
        }

    def start_watcher(self) -> WatcherControlResponse:
        self.watcher.start()
        return WatcherControlResponse(ok=True, status="running", summary="watcher 已启动")

    def stop_watcher(self) -> WatcherControlResponse:
        self.watcher.stop()
        return WatcherControlResponse(ok=True, status="stopped", summary="watcher 已停止")

    def switch_session(self, payload: SwitchSessionRequest) -> dict:
        result = self.navigator.switch_to_session(payload.session_title)
        self._record_event("action.switch_session", {"session_title": payload.session_title, "result": result})
        return result

    def switch_first_unread_session(self, payload: SwitchFirstUnreadRequest) -> dict:
        unread_sessions = self.list_unread_sessions(chat_type_hint=payload.chat_type_hint)
        if not unread_sessions:
            result = {
                "ok": False,
                "status": "no_unread_session",
                "summary": "未发现符合条件的未读会话",
                "chat_type_hint": payload.chat_type_hint,
            }
            self._record_event("action.switch_first_unread_session", {"chat_type_hint": payload.chat_type_hint, "result": result})
            return result

        target = unread_sessions[0]
        result = self.navigator.switch_to_session(target["title"])
        result["selected_session"] = target
        self._record_event(
            "action.switch_first_unread_session",
            {"chat_type_hint": payload.chat_type_hint, "selected_session": target, "result": result},
        )
        return result

    def fetch_first_unread_incremental(self, payload: FetchFirstUnreadIncrementalRequest) -> dict:
        unread_sessions = self.list_unread_sessions(chat_type_hint=payload.chat_type_hint)
        if not unread_sessions:
            result = {
                "ok": False,
                "status": "no_unread_session",
                "summary": "未发现符合条件的未读会话",
                "chat_type_hint": payload.chat_type_hint,
                "conversation": None,
                "messages": [],
                "new_messages": [],
                "summary_detail": {"message_count": 0, "new_message_count": 0},
            }
            self._record_event(
                "action.fetch_first_unread_incremental",
                {"chat_type_hint": payload.chat_type_hint, "result": result},
            )
            return result

        target = unread_sessions[0]
        switch_result = self.navigator.switch_to_session(target["title"])
        if not switch_result.get("ok"):
            result = {
                "ok": False,
                "status": "switch_failed",
                "summary": "未读会话已识别，但切换失败",
                "chat_type_hint": payload.chat_type_hint,
                "selected_session": target,
                "switch_result": switch_result,
                "conversation": None,
                "messages": [],
                "new_messages": [],
                "summary_detail": {"message_count": 0, "new_message_count": 0},
            }
            self._record_event(
                "action.fetch_first_unread_incremental",
                {"chat_type_hint": payload.chat_type_hint, "selected_session": target, "result": result},
            )
            return result

        time.sleep(float(payload.switch_wait_seconds or 0.0))
        incremental = self.get_current_chat_incremental(mark_as_seen=payload.mark_as_seen)
        self._apply_direct_unread_sender(
            conversation=incremental.get("conversation"),
            new_messages=incremental.get("new_messages", []),
        )
        result = {
            "ok": bool(incremental.get("ok")),
            "status": "fetched",
            "summary": "已切入首个未读会话并读取增量消息",
            "chat_type_hint": payload.chat_type_hint,
            "selected_session": target,
            "switch_result": switch_result,
            "conversation": incremental.get("conversation"),
            "messages": incremental.get("messages", []),
            "new_messages": incremental.get("new_messages", []),
            "summary_detail": incremental.get("summary", {}),
            "meta": incremental.get("meta", {}),
        }
        self._record_event(
            "action.fetch_first_unread_incremental",
            {"chat_type_hint": payload.chat_type_hint, "selected_session": target, "result": result},
        )
        return result

    def _apply_direct_unread_sender(self, *, conversation: dict | None, new_messages: list[dict]) -> None:
        conversation_payload = conversation if isinstance(conversation, dict) else {}
        chat_type = str(conversation_payload.get("chat_type") or "").strip().lower()
        chat_title = str(conversation_payload.get("title") or "").strip()
        if chat_type != "direct" or not chat_title:
            return
        for item in new_messages:
            if not isinstance(item, dict):
                continue
            if item.get("sender_name"):
                continue
            item["sender_name"] = chat_title
            if str(item.get("direction") or "").strip() in {"", "unknown"}:
                item["direction"] = "in"

    def send_text(self, payload: SendTextRequest) -> dict:
        result = self.sender.send_text(payload.session_title, payload.content)
        self._record_event("action.send_text", {"session_title": payload.session_title, "content": payload.content, "result": result})
        return result

    def send_file(self, payload: SendFileRequest) -> dict:
        result = self.sender.send_file(payload.session_title, payload.file_path)
        self._record_event("action.send_file", {"session_title": payload.session_title, "file_path": payload.file_path, "result": result})
        return result

    def recent_events(self) -> list[dict]:
        return self.event_store.recent(limit=50)

    def _record_event(self, event_type: str, details: dict) -> None:
        raw = f"{event_type}|{details}|{datetime.now(timezone.utc).isoformat()}"
        event = ProviderEventModel(
            event_id=hashlib.sha1(raw.encode("utf-8")).hexdigest(),
            event_type=event_type,
            details=details,
        )
        self.event_store.append(event.model_dump(mode="json"))

    def _safe_collect_sessions(self):
        try:
            return self.session_collector.collect(), None
        except Exception as exc:
            logger.warning("session collection failed", exc_info=True)
            return [], str(exc)

    def _safe_get_conversation(self):
        try:
            return self.current_chat_collector.get_conversation(), None
        except Exception as exc:
            logger.warning("conversation collection failed", exc_info=True)
            return None, str(exc)

    def _safe_get_messages(self):
        try:
            return self.current_chat_collector.get_messages(), None
        except Exception as exc:
            logger.warning("message collection failed", exc_info=True)
            return [], str(exc)


_runtime: ProviderRuntime | None = None


def get_runtime() -> ProviderRuntime:
    global _runtime
    if _runtime is None:
        logger.info("initializing provider runtime")
        _runtime = ProviderRuntime()
    return _runtime
