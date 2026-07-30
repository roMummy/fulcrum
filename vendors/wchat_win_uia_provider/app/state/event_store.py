from __future__ import annotations

import json
from pathlib import Path

from ..core.config import settings


class EventStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.state_dir / settings.event_file

    def append(self, payload: dict) -> None:
        with self.event_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def recent(self, *, limit: int = 50) -> list[dict]:
        if not self.event_path.exists():
            return []
        lines = self.event_path.read_text(encoding="utf-8").splitlines()
        items: list[dict] = []
        for line in lines[-limit:]:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                items.append(parsed)
        return items
