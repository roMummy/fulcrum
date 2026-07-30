from __future__ import annotations

import hashlib
import logging
import threading
import time
from datetime import datetime, timezone

from ..core.models import ProviderEventModel, WatcherStateModel
from ..state.event_store import EventStore
from ..state.preview_store import PreviewStore
from ..state.runtime_state import RuntimeState
from ..state.watcher_state_store import WatcherStateStore

logger = logging.getLogger(__name__)


class SessionListWatcher:
    def __init__(
        self,
        *,
        session_collector,
        event_store: EventStore,
        preview_store: PreviewStore,
        runtime_state: RuntimeState,
        watcher_state_store: WatcherStateStore,
        poll_interval_seconds: float,
    ) -> None:
        self.session_collector = session_collector
        self.event_store = event_store
        self.preview_store = preview_store
        self.runtime_state = runtime_state
        self.watcher_state_store = watcher_state_store
        self.poll_interval_seconds = poll_interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self.runtime_state.watcher_running:
            return
        self._stop_event.clear()
        self.runtime_state.watcher_running = True
        self.runtime_state.watcher_started_at = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._run_loop, name="session-list-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self.runtime_state.watcher_running = False
        self.runtime_state.watcher_stopped_at = datetime.now(timezone.utc)

    def _run_loop(self) -> None:
        previous = self.watcher_state_store.load_state().fingerprint
        while not self._stop_event.is_set():
            try:
                self.runtime_state.last_watcher_poll_at = datetime.now(timezone.utc)
                sessions = self.session_collector.collect()
                current = self._build_fingerprint(sessions)
                self.preview_store.save_sessions(sessions)
                for event in self._diff_events(previous, current):
                    self.event_store.append(event.model_dump(mode="json"))
                poll_event = ProviderEventModel(
                    event_id=hashlib.sha1(
                        f"watcher.poll|{self.runtime_state.last_watcher_poll_at.isoformat()}|{len(sessions)}".encode("utf-8")
                    ).hexdigest(),
                    event_type="watcher.poll",
                    details={"session_count": len(sessions)},
                )
                self.event_store.append(poll_event.model_dump(mode="json"))
                self.watcher_state_store.save_state(
                    WatcherStateModel(
                        updated_at=self.runtime_state.last_watcher_poll_at,
                        fingerprint=current,
                        count=len(current),
                    )
                )
                previous = current
            except Exception as exc:
                logger.warning("watcher poll failed", exc_info=True)
                error_event = ProviderEventModel(
                    event_id=hashlib.sha1(
                        f"watcher.error|{datetime.now(timezone.utc).isoformat()}|{str(exc)}".encode("utf-8")
                    ).hexdigest(),
                    event_type="watcher.error",
                    details={"error": str(exc)},
                )
                self.event_store.append(error_event.model_dump(mode="json"))
            time.sleep(self.poll_interval_seconds)

    def _build_fingerprint(self, sessions) -> dict[str, dict]:
        fingerprint: dict[str, dict] = {}
        for item in sessions:
            fingerprint[item.title] = {
                "preview": item.preview,
                "unread_hint": item.unread_hint,
                "timestamp": item.timestamp_text,
                "chat_type_hint": item.chat_type_hint,
            }
        return fingerprint

    def _diff_events(self, previous: dict[str, dict], current: dict[str, dict]) -> list[ProviderEventModel]:
        events: list[ProviderEventModel] = []
        created_at = datetime.now(timezone.utc)
        previous_titles = set(previous.keys())
        current_titles = set(current.keys())
        for title in sorted(current_titles - previous_titles):
            events.append(self._build_event("session_seen", title, current[title], created_at))
        for title in sorted(previous_titles - current_titles):
            events.append(self._build_event("session_disappeared", title, previous[title], created_at))
        for title in sorted(previous_titles & current_titles):
            before = previous[title]
            after = current[title]
            if before != after:
                event_type = "session_preview_changed" if before.get("preview") != after.get("preview") else "session_changed"
                events.append(self._build_event(event_type, title, after, created_at))
        return events

    def _build_event(self, event_type: str, title: str, payload: dict, created_at: datetime) -> ProviderEventModel:
        raw = f"{event_type}|{title}|{payload}|{created_at.isoformat()}"
        return ProviderEventModel(
            event_id=hashlib.sha1(raw.encode("utf-8")).hexdigest(),
            event_type=event_type,
            created_at=created_at,
            details={
                "chat": title,
                "preview": payload.get("preview"),
                "unread_hint": payload.get("unread_hint"),
                "timestamp": payload.get("timestamp"),
                "chat_type_hint": payload.get("chat_type_hint"),
            },
        )
