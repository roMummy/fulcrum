from __future__ import annotations

import json
from pathlib import Path

from ..core.config import settings


class PreviewStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.preview_path = self.state_dir / settings.session_preview_file

    def save_sessions(self, sessions) -> None:
        items = [item.model_dump(mode="json") for item in sessions]
        self.preview_path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
