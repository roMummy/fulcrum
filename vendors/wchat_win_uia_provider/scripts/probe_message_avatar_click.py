from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uiautomation as auto
from pywinauto import Desktop
from pywinauto.mouse import click


MAIN_WINDOW_CLASS = "mmui::MainWindow"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "output" / "uiautomation_message_avatar_probe.json"


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
    window = _find_main_window()
    if window is None:
        return {"ok": False, "error": "WECHAT_WINDOW_NOT_FOUND"}

    message_list = _find_by_auto_id(window, "chat_message_list")
    if message_list is None:
        return {"ok": False, "error": "CHAT_MESSAGE_LIST_NOT_FOUND"}

    candidates = _collect_message_candidates(message_list)
    target = _pick_target_candidate(candidates, message_list)
    if target is None:
        return {
            "ok": False,
            "error": "AVATAR_TARGET_NOT_FOUND",
            "candidates": candidates,
        }

    probe_points = _build_probe_points(message_list=message_list, target=target)
    attempts = _run_avatar_attempts(window=window, target=target, probe_points=probe_points)

    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": _control_payload(window),
        "message_list": _control_payload(message_list),
        "target": target,
        "probe_index": _probe_index(),
        "probe_points": probe_points,
        "attempts": attempts,
        "candidates": candidates[:12],
    }


def _find_main_window():
    desktop = Desktop(backend="uia")
    for item in desktop.windows():
        info = getattr(item, "element_info", None)
        class_name = str(getattr(info, "class_name", "") or "").strip()
        if class_name == MAIN_WINDOW_CLASS:
            return item
    return None


def _find_by_auto_id(control: Any, auto_id: str):
    for child in control.descendants():
        info = getattr(child, "element_info", None)
        if str(getattr(info, "automation_id", "") or "").strip() == auto_id:
            return child
    return None


def _collect_message_candidates(message_list: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    list_rect = _rect_payload(message_list)
    list_center_x = ((list_rect["left"] + list_rect["right"]) // 2) if list_rect else None
    for child in message_list.descendants():
        info = getattr(child, "element_info", None)
        class_name = str(getattr(info, "class_name", "") or "").strip()
        auto_id = str(getattr(info, "automation_id", "") or "").strip()
        if class_name not in {"mmui::ChatTextItemView", "mmui::ChatBubbleItemView"} and "chat_bubble_item_view" not in auto_id:
            continue
        rect = _rect_payload(child)
        if rect is None:
            continue
        text = _read_text(child)
        direction_hint = _direction_hint(rect=rect, list_center_x=list_center_x)
        items.append(
            {
                "name": _safe_str(getattr(info, "name", None)),
                "class_name": class_name or None,
                "auto_id": auto_id or None,
                "text": text or None,
                "rect": rect,
                "direction_hint": direction_hint,
            }
        )
    return items


def _pick_target_candidate(candidates: list[dict[str, Any]], message_list: Any) -> dict[str, Any] | None:
    visible = [item for item in candidates if item.get("rect")]
    incoming = [item for item in visible if item.get("direction_hint") == "incoming"]
    if incoming:
        return incoming[-1]
    return visible[-1] if visible else None


def _direction_hint(*, rect: dict[str, int], list_center_x: int | None) -> str:
    if list_center_x is None:
        return "unknown"
    bubble_center_x = (int(rect["left"]) + int(rect["right"])) // 2
    return "incoming" if bubble_center_x <= list_center_x else "outgoing"


def _run_avatar_attempts(*, window: Any, target: dict[str, Any], probe_points: list[dict[str, int | str]]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    probe_index = _probe_index()
    if not probe_points:
        return attempts
    if probe_index < 1 or probe_index > len(probe_points):
        probe_index = 1
    point = probe_points[probe_index - 1]
    _focus_wechat_window(window)
    before_windows = _top_level_windows()
    before_focus = _focused_control_payload()
    click(coords=(point["x"], point["y"]))
    time.sleep(1.0)
    after_windows = _top_level_windows()
    after_focus = _focused_control_payload()
    popup_related = _collect_popup_related(limit=80)
    attempts.append(
        {
            "label": point["label"],
            "coords": {"x": point["x"], "y": point["y"]},
            "before_focus": before_focus,
            "after_focus": after_focus,
            "before_windows": before_windows,
            "after_windows": after_windows,
            "new_windows": _diff_windows(before_windows, after_windows),
            "popup_related": popup_related,
        }
    )
    return attempts


def _build_probe_points(*, message_list: Any, target: dict[str, Any]) -> list[dict[str, int | str]]:
    rect = target.get("rect") or {}
    list_rect = _rect_payload(message_list) or {}
    left_bound = int(list_rect.get("left", rect.get("left", 0))) + 6
    right_bound = int(list_rect.get("right", rect.get("right", 0))) - 6
    top = int(rect.get("top", 0))
    bottom = int(rect.get("bottom", top + 60))
    height = max(bottom - top, 40)
    direction_hint = str(target.get("direction_hint") or "incoming")

    y_offsets = [12, 20, 28]
    if direction_hint == "outgoing":
        x_candidates = [right_bound - offset for offset in (12, 20, 28, 36)]
        prefix = "outgoing_top"
    else:
        # Keep the probe strictly inside the message pane and close to the row's top-left corner.
        x_candidates = [left_bound + offset for offset in (12, 20, 28, 36)]
        prefix = "incoming_top"

    points: list[dict[str, int | str]] = []
    seen: set[tuple[int, int]] = set()
    for x in x_candidates:
        for y_offset in y_offsets:
            y = top + y_offset
            key = (int(x), int(y))
            if key in seen:
                continue
            seen.add(key)
            points.append({"label": f"{prefix}_{len(points)+1}", "x": int(x), "y": int(y)})
    return points


def _focus_wechat_window(window: Any) -> None:
    try:
        window.set_focus()
    except Exception:
        pass
    time.sleep(0.15)


def _top_level_windows() -> list[dict[str, Any]]:
    root = auto.GetRootControl()
    return [_control_payload(child) for child in root.GetChildren()]


def _collect_popup_related(*, limit: int) -> list[dict[str, Any]]:
    root = auto.GetRootControl()
    items: list[dict[str, Any]] = []
    keywords = ("昵称", "微信号", "地区", "群成员", "聊天信息", "头像", "资料", "名片")
    for child in _iter_descendants(root):
        payload = _control_payload(child)
        if not any(payload.get(key) for key in ("name", "class_name", "automation_id", "text")):
            continue
        blob = json.dumps(payload, ensure_ascii=False)
        if any(keyword in blob for keyword in keywords):
            items.append(payload)
    return items[: max(limit, 1)]


def _focused_control_payload() -> dict[str, Any] | None:
    try:
        focused = auto.GetFocusedControl()
    except Exception:
        focused = None
    if focused is None:
        return None
    return _control_payload(focused)


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


def _read_text(control: Any) -> str:
    parts: list[str] = []
    try:
        text = str(control.window_text() or "").strip()
        if text:
            parts.append(text)
    except Exception:
        pass
    try:
        texts = control.texts()
    except Exception:
        texts = []
    for item in texts or []:
        value = str(item or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts).strip()


def _control_payload(control) -> dict[str, Any]:
    if control is None:
        return {}
    name = _safe_str(getattr(control, "Name", None))
    class_name = _safe_str(getattr(control, "ClassName", None))
    automation_id = _safe_str(getattr(control, "AutomationId", None))
    control_type = _safe_str(getattr(control, "ControlTypeName", None))
    localized_control_type = _safe_str(getattr(control, "LocalizedControlType", None))
    rect = _rect_payload(control)
    return {
        "name": name,
        "class_name": class_name,
        "automation_id": automation_id,
        "control_type": control_type,
        "localized_control_type": localized_control_type,
        "text": name,
        "rect": rect,
    }


def _rect_payload(control_or_rect: Any) -> dict[str, int] | None:
    rect = getattr(control_or_rect, "BoundingRectangle", None)
    if rect is None and hasattr(control_or_rect, "rectangle"):
        try:
            rect = control_or_rect.rectangle()
        except Exception:
            rect = None
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


def _probe_index() -> int:
    raw = str(os.environ.get("WECHAT_UIA_AVATAR_PROBE_INDEX", "1") or "1").strip()
    try:
        return max(int(raw), 1)
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
