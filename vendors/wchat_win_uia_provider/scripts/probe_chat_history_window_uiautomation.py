from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uiautomation as auto


TARGET_CLASS = "mmui::SearchMsgUniqueChatWindow"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "output" / "uiautomation_chat_history_window_probe.json"


def main() -> int:
    try:
        payload = run_probe()
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": payload.get("ok", False), "output_path": str(OUTPUT_PATH)}, ensure_ascii=False))
    return 0


def run_probe() -> dict[str, Any]:
    window = _find_history_window()
    if window is None:
        return {"ok": False, "error": "CHAT_HISTORY_WINDOW_NOT_FOUND"}
    descendants = _collect_descendants(window, limit=800)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": _control_payload(window),
        "tree": _dump_tree(window, depth=0, max_depth=6, max_children=25),
        "descendants": descendants,
        "message_like_items": [
            item
            for item in descendants
            if any(
                token in json.dumps(item, ensure_ascii=False)
                for token in ("引用", "消息", "发送者", "聊天记录", "搜索", "日期", "群成员", ":")
            )
        ],
    }


def _find_history_window():
    root = auto.GetRootControl()
    for child in root.GetChildren():
        class_name = _safe_str(getattr(child, "ClassName", None))
        if class_name == TARGET_CLASS:
            return child
    return None


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


def _dump_tree(control, *, depth: int, max_depth: int, max_children: int) -> dict[str, Any] | None:
    if control is None:
        return None
    payload = _control_payload(control)
    payload["depth"] = depth
    payload["children"] = []
    if depth >= max_depth:
        return payload
    try:
        children = list(control.GetChildren())
    except Exception:
        children = []
    for child in children[: max(max_children, 1)]:
        child_payload = _dump_tree(child, depth=depth + 1, max_depth=max_depth, max_children=max_children)
        if child_payload is not None:
            payload["children"].append(child_payload)
    payload["child_count"] = len(children)
    return payload


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
