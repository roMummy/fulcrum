from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import uiautomation as auto
except ImportError as exc:  # pragma: no cover - runtime dependency probe
    print(json.dumps({"ok": False, "error": f"uiautomation import failed: {exc}"}), file=sys.stderr)
    raise


MAIN_WINDOW_CLASS = "mmui::MainWindow"
MAIN_WINDOW_TITLE = "微信"
MESSAGE_PAGE_AUTO_ID = "chat_message_page"
MESSAGE_LIST_AUTO_ID = "chat_message_list"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "output" / "uiautomation_message_probe.json"


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
    message_page = _find_first(window, lambda c: _safe_str(getattr(c, "AutomationId", None)) == MESSAGE_PAGE_AUTO_ID)
    message_list = _find_first(window, lambda c: _safe_str(getattr(c, "AutomationId", None)) == MESSAGE_LIST_AUTO_ID)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": _control_payload(window),
        "message_page": _control_payload(message_page) if message_page else None,
        "message_page_tree": _dump_tree(message_page, depth=0, max_depth=5, max_children=20) if message_page else None,
        "message_list": _control_payload(message_list) if message_list else None,
        "message_list_tree": _dump_tree(message_list, depth=0, max_depth=5, max_children=20) if message_list else None,
        "message_contexts": _collect_message_contexts(message_list, limit=20) if message_list else [],
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


def _find_first(control, predicate):
    if control is None:
        return None
    stack = [control]
    while stack:
        current = stack.pop()
        try:
            children = list(current.GetChildren())
        except Exception:
            children = []
        for child in children:
            if predicate(child):
                return child
            stack.append(child)
    return None


def _collect_message_contexts(message_list, *, limit: int) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for child in _iter_descendants(message_list):
        class_name = _safe_str(getattr(child, "ClassName", None)) or ""
        auto_id = _safe_str(getattr(child, "AutomationId", None)) or ""
        if class_name in {"mmui::ChatTextItemView", "mmui::ChatBubbleItemView", "mmui::ChatBubbleReferItemView"}:
            candidates.append(child)
            continue
        if "chat_bubble_item_view" in auto_id:
            candidates.append(child)
    items: list[dict[str, Any]] = []
    for child in candidates[: max(limit, 1)]:
        parent_chain = []
        parent = child
        for _ in range(6):
            try:
                parent = parent.GetParentControl()
            except Exception:
                parent = None
            if parent is None:
                break
            parent_chain.append(_control_payload(parent))
        try:
            parent = child.GetParentControl()
            siblings = list(parent.GetChildren()) if parent else []
        except Exception:
            siblings = []
        items.append(
            {
                "message": _control_payload(child),
                "parent_chain": parent_chain,
                "siblings": [_control_payload(item) for item in siblings[:12]],
            }
        )
    return items


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
        "text": _safe_name(control),
        "rect": _rect_payload(getattr(control, "BoundingRectangle", None)),
    }


def _safe_name(control) -> str | None:
    try:
        value = getattr(control, "Name", None)
    except Exception:
        value = None
    return _safe_str(value)


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
