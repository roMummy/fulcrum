from __future__ import annotations

import json
from pathlib import Path

from ..core.config import settings
from ..core.models import WatcherStateModel


class WatcherStateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / settings.watcher_state_file

    def load_state(self) -> WatcherStateModel:
        if not self.state_path.exists():
            return WatcherStateModel()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return WatcherStateModel()
        if not isinstance(payload, dict):
            return WatcherStateModel()
        try:
            return WatcherStateModel.model_validate(payload)
        except Exception:
            return WatcherStateModel()

    def save_state(self, state: WatcherStateModel) -> None:
        self.state_path.write_text(
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
