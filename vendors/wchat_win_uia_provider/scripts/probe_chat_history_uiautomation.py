from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uiautomation as auto


MAIN_WINDOW_CLASS = "mmui::MainWindow"
MAIN_WINDOW_TITLE = "微信"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "output" / "uiautomation_chat_history_probe.json"


def main() -> int:
    payload = run_probe()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": payload.get("ok", False), "output_path": str(OUTPUT_PATH)}, ensure_ascii=False))
    return 0


def run_probe() -> dict[str, Any]:
    window = _find_main_window()
    if window is None:
        return {"ok": False, "error": "WECHAT_WINDOW_NOT_FOUND"}

    before_windows = _top_level_windows()
    before_descendants = _collect_descendants(window, limit=250)
    history_button = _find_chat_history_button(window)
    if history_button is None:
        return {
            "ok": False,
            "error": "CHAT_HISTORY_BUTTON_NOT_FOUND",
            "before_windows": before_windows,
            "before_descendants": before_descendants,
        }

    click_result = _click_control(history_button)
    time.sleep(3.0)
    after_windows = _top_level_windows()
    after_descendants = _collect_descendants(window, limit=400)

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": _control_payload(window),
        "history_button": _control_payload(history_button),
        "click_result": click_result,
        "before_windows": before_windows,
        "after_windows": after_windows,
        "before_descendants": before_descendants,
        "after_descendants": after_descendants,
        "new_windows": _diff_windows(before_windows, after_windows),
        "new_descendants": _diff_descendants(before_descendants, after_descendants),
        "history_related_after": [
            item
            for item in after_descendants
            if any(token in json.dumps(item, ensure_ascii=False) for token in ("聊天记录", "chatlog", "record", "history"))
        ],
    }


def _find_main_window():
    root = auto.GetRootControl()
    for child in root.GetChildren():
        class_name = _safe_str(getattr(child, "ClassName", None))
        name = _safe_str(getattr(child, "Name", None))
        if class_name == MAIN_WINDOW_CLASS:
            return child
        if name == MAIN_WINDOW_TITLE and (not class_name or class_name == MAIN_WINDOW_CLASS):
            return child
    return None


def _find_chat_history_button(window):
    button_match = None
    for child in _iter_descendants(window):
        payload = _control_payload(child)
        blob = json.dumps(payload, ensure_ascii=False)
        if payload.get("name") == "聊天记录" and payload.get("control_type") == "ButtonControl":
            return child
        if ("聊天记录" in blob or "chatlog" in blob.lower()) and button_match is None:
            button_match = child
    return button_match


def _click_control(control) -> dict[str, Any]:
    result = {"invoke": False, "click": False}
    try:
        control.Invoke()
        result["invoke"] = True
        return result
    except Exception as exc:
        result["invoke_error"] = str(exc)
    try:
        control.Click(simulateMove=False)
        result["click"] = True
    except Exception as exc:
        result["click_error"] = str(exc)
    return result


def _top_level_windows() -> list[dict[str, Any]]:
    root = auto.GetRootControl()
    return [_control_payload(child) for child in root.GetChildren()]


def _collect_descendants(control, *, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for child in _iter_descendants(control):
        payload = _control_payload(child)
        if any(payload.get(key) for key in ("name", "class_name", "automation_id", "text")):
            items.append(payload)
    return items[: max(limit, 1)]


def _iter_descendants(control):
    if control is None:
        return
    stack = [control]
    while stack:
        current = stack.pop()
        try:
            children = list(current.GetChildren())
        except Exception:
            children = []
        for child in children:
            yield child
            stack.append(child)


def _diff_windows(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_keys = {_window_key(item) for item in before}
    return [item for item in after if _window_key(item) not in before_keys]


def _diff_descendants(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_keys = {_window_key(item) for item in before}
    return [item for item in after if _window_key(item) not in before_keys]


def _window_key(item: dict[str, Any]) -> tuple:
    rect = item.get("rect") or {}
    return (
        item.get("name"),
        item.get("class_name"),
        item.get("automation_id"),
        rect.get("left"),
        rect.get("top"),
        rect.get("right"),
        rect.get("bottom"),
    )


def _control_payload(control) -> dict[str, Any]:
    if control is None:
        return {}
    return {
        "name": _safe_str(getattr(control, "Name", None)),
        "class_name": _safe_str(getattr(control, "ClassName", None)),
        "automation_id": _safe_str(getattr(control, "AutomationId", None)),
        "control_type": _safe_str(getattr(control, "ControlTypeName", None)),
        "localized_control_type": _safe_str(getattr(control, "LocalizedControlType", None)),
        "text": _safe_str(getattr(control, "Name", None)),
        "rect": _rect_payload(getattr(control, "BoundingRectangle", None)),
    }


def _rect_payload(rect: Any) -> dict[str, int] | None:
    if rect is None:
        return None
    left = _safe_int(getattr(rect, "left", None))
    top = _safe_int(getattr(rect, "top", None))
    right = _safe_int(getattr(rect, "right", None))
    bottom = _safe_int(getattr(rect, "bottom", None))
    if None in {left, top, right, bottom}:
        return None
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _safe_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    return normalized or None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
