from __future__ import annotations

import json
from pathlib import Path

from pywinauto import Desktop


OUTPUT_PATH = Path("data") / "debug_windows.json"


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    desktop = Desktop(backend="uia")
    items: list[dict] = []
    for window in desktop.windows():
        info = getattr(window, "element_info", None)
        try:
            rect = window.rectangle()
            rect_payload = [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
        except Exception:
            rect_payload = None
        items.append(
            {
                "title": str(getattr(info, "name", "") or "").strip() or str(window.window_text() or "").strip(),
                "class_name": str(getattr(info, "class_name", "") or "").strip(),
                "automation_id": str(getattr(info, "automation_id", "") or "").strip(),
                "control_type": str(getattr(info, "control_type", "") or "").strip(),
                "handle": int(getattr(info, "handle", 0) or 0),
                "rect": rect_payload,
            }
        )
    payload = {
        "count": len(items),
        "items": items,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved {len(items)} windows to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
