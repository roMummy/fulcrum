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
MESSAGE_LIST_AUTO_ID = "chat_message_list"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "output" / "uiautomation_message_rows_probe.json"


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
    message_list = _find_first(window, lambda c: _safe_str(getattr(c, "AutomationId", None)) == MESSAGE_LIST_AUTO_ID)
    if message_list is None:
        return {"ok": False, "error": "CHAT_MESSAGE_LIST_NOT_FOUND"}

    rows = _collect_rows(message_list)
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": _control_payload(window),
        "message_list": _control_payload(message_list),
        "rows": rows,
        "summary": {
            "row_count": len(rows),
            "message_block_count": len({row["block_index"] for row in rows if row.get("block_index") is not None}),
            "text_row_count": sum(1 for row in rows if row.get("row_type") == "text"),
            "time_row_count": sum(1 for row in rows if row.get("row_type") == "time"),
            "media_row_count": sum(1 for row in rows if row.get("row_type") == "media"),
            "system_row_count": sum(1 for row in rows if row.get("row_type") == "system"),
        },
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


def _collect_rows(message_list) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    list_rect = _rect_payload(getattr(message_list, "BoundingRectangle", None))
    list_center_x = None
    if list_rect is not None:
        list_center_x = (list_rect["left"] + list_rect["right"]) // 2

    candidates: list[Any] = []
    for child in message_list.GetChildren():
        class_name = _safe_str(getattr(child, "ClassName", None)) or ""
        auto_id = _safe_str(getattr(child, "AutomationId", None)) or ""
        if class_name in {"mmui::ChatTextItemView", "mmui::ChatBubbleItemView", "mmui::ChatBubbleReferItemView", "mmui::ChatItemView"}:
            candidates.append(child)
            continue
        if "chat_bubble_item_view" in auto_id:
            candidates.append(child)

    enriched: list[dict[str, Any]] = []
    for index, child in enumerate(candidates):
        payload = _control_payload(child)
        rect = payload.get("rect") or {}
        top = rect.get("top")
        left = rect.get("left")
        right = rect.get("right")
        width = (right - left) if isinstance(left, int) and isinstance(right, int) else None
        height = (rect.get("bottom") - rect.get("top")) if isinstance(rect.get("bottom"), int) and isinstance(top, int) else None
        center_x = ((left + right) // 2) if isinstance(left, int) and isinstance(right, int) else None
        row_type = _classify_row(payload)
        text = str(payload.get("text") or "").strip()
        direction_hint = _direction_hint(center_x=center_x, list_center_x=list_center_x, row_type=row_type)
        enriched.append(
            {
                "row_index": index,
                "row_type": row_type,
                "direction_hint": direction_hint,
                "text": text or None,
                "class_name": payload.get("class_name"),
                "auto_id": payload.get("automation_id"),
                "control_type": payload.get("control_type"),
                "rect": payload.get("rect"),
                "width": width,
                "height": height,
                "top_gap_from_prev": None,
                "block_index": None,
                "block_role": None,
            }
        )

    block_index = -1
    previous_non_time: dict[str, Any] | None = None
    for index, row in enumerate(enriched):
        current_top = ((row.get("rect") or {}).get("top"))
        if index > 0:
            prev_bottom = ((enriched[index - 1].get("rect") or {}).get("bottom"))
            if isinstance(current_top, int) and isinstance(prev_bottom, int):
                row["top_gap_from_prev"] = current_top - prev_bottom

        if row["row_type"] == "time":
            row["block_role"] = "separator"
            previous_non_time = None
            continue

        start_new_block = previous_non_time is None
        if not start_new_block:
            prev_gap = row.get("top_gap_from_prev")
            if isinstance(prev_gap, int) and prev_gap >= 18:
                start_new_block = True
            if row.get("direction_hint") != previous_non_time.get("direction_hint"):
                start_new_block = True
        if start_new_block:
            block_index += 1
        row["block_index"] = block_index
        row["block_role"] = "message"
        previous_non_time = row

    return enriched


def _classify_row(payload: dict[str, Any]) -> str:
    class_name = str(payload.get("class_name") or "").strip()
    text = str(payload.get("text") or "").strip()
    lowered = text.lower()
    if class_name == "mmui::ChatItemView":
        return "time"
    if "加入群聊" in text or "撤回" in text or "以下为新消息" in text:
        return "system"
    if class_name in {"mmui::ChatBubbleReferItemView", "mmui::ChatBubbleItemView"}:
        return "media"
    if class_name == "mmui::ChatTextItemView":
        if any(lowered.endswith(ext) for ext in (".xls", ".xlsx", ".doc", ".docx", ".pdf", ".zip", ".rar")):
            return "file"
        return "text"
    return "unknown"


def _direction_hint(*, center_x: int | None, list_center_x: int | None, row_type: str) -> str:
    if row_type == "time":
        return "center"
    if center_x is None or list_center_x is None:
        return "unknown"
    return "incoming" if center_x <= list_center_x else "outgoing"


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
