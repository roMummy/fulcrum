from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RuntimeState:
    watcher_running: bool = False
    watcher_started_at: datetime | None = None
    watcher_stopped_at: datetime | None = None
    last_watcher_poll_at: datetime | None = None
    last_snapshot_at: datetime | None = None

    def iso_or_none(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def as_dict(self) -> dict:
        return {
            "watcher_running": self.watcher_running,
            "watcher_started_at": self.iso_or_none(self.watcher_started_at),
            "watcher_stopped_at": self.iso_or_none(self.watcher_stopped_at),
            "last_watcher_poll_at": self.iso_or_none(self.last_watcher_poll_at),
            "last_snapshot_at": self.iso_or_none(self.last_snapshot_at),
        }
