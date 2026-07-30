from __future__ import annotations

import json
from pathlib import Path

from ..core.config import settings


class SnapshotStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.state_dir / settings.snapshot_file

    def save_snapshot(self, payload: dict) -> None:
        self.snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
