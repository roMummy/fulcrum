from __future__ import annotations

from typing import Any

from ..core.models import RectModel, SessionPreviewModel
from .window_finder import WindowFinder


class SessionListCollector:
    def __init__(self, window_finder: WindowFinder) -> None:
        self.window_finder = window_finder

    def collect(self) -> list:
        window = self.window_finder.get_main_window()
        session_list = self._find_by_auto_id(window, "session_list")
        if session_list is None:
            return []

        items: list[SessionPreviewModel] = []
        for child in session_list.descendants():
            info = getattr(child, "element_info", None)
            class_name = str(getattr(info, "class_name", "") or "").strip()
            auto_id = str(getattr(info, "automation_id", "") or "").strip()
            if class_name != "mmui::ChatSessionCell" and not auto_id.startswith("session_item_"):
                continue
            parsed = self._parse_session_item(child)
            if parsed:
                items.append(parsed)
        return items

    def _parse_session_item(self, control: Any) -> SessionPreviewModel | None:
        raw_text = self._control_text(control)
        lines = [line.strip() for line in raw_text.splitlines() if line and line.strip()]
        if not lines:
            return None

        title = lines[0]
        unread_hint = None
        preview = None
        timestamp_text = None
        remaining = lines[1:]
        if remaining and remaining[0].startswith("[") and remaining[0].endswith("]"):
            unread_hint = remaining[0]
            remaining = remaining[1:]
        if remaining:
            preview = remaining[0]
        if len(remaining) >= 2:
            timestamp_text = remaining[-1]
        elif remaining and self._looks_like_time(remaining[0]):
            timestamp_text = remaining[0]
            preview = None

        info = getattr(control, "element_info", None)
        auto_id = str(getattr(info, "automation_id", "") or "").strip() or None
        if not title and auto_id and auto_id.startswith("session_item_"):
            title = auto_id.removeprefix("session_item_")
        if not title:
            return None
        chat_type_hint = self._guess_chat_type(title=title, preview=preview)
        return SessionPreviewModel(
            session_id=self._build_session_id(title=title, chat_type=chat_type_hint),
            title=title,
            preview=preview,
            timestamp_text=timestamp_text,
            unread_hint=unread_hint,
            has_new=bool(unread_hint),
            chat_type_hint=chat_type_hint,
            rect=self._rect_from_control(control),
            raw_text=raw_text or None,
            auto_id=auto_id,
        )

    def _build_session_id(self, *, title: str, chat_type: str | None) -> str:
        normalized_type = str(chat_type or "").strip().lower()
        if normalized_type not in {"group", "direct"}:
            normalized_type = "unknown"
        return f"uia:{normalized_type}:{title}"

    def _find_by_auto_id(self, control: Any, auto_id: str):
        for child in control.descendants():
            info = getattr(child, "element_info", None)
            if str(getattr(info, "automation_id", "") or "").strip() == auto_id:
                return child
        return None

    def _control_text(self, control: Any) -> str:
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
        if parts:
            return "\n".join(parts).strip()

        descendants_text: list[str] = []
        try:
            descendants = control.descendants()
        except Exception:
            descendants = []
        for child in descendants:
            try:
                child_text = str(child.window_text() or "").strip()
            except Exception:
                child_text = ""
            if child_text and child_text not in descendants_text:
                descendants_text.append(child_text)
        return "\n".join(descendants_text).strip()

    def _rect_from_control(self, control: Any) -> RectModel | None:
        try:
            rect = control.rectangle()
        except Exception:
            return None
        return RectModel(left=int(rect.left), top=int(rect.top), right=int(rect.right), bottom=int(rect.bottom))

    def _looks_like_time(self, value: str) -> bool:
        text = str(value or "").strip()
        return bool(text) and ":" in text and len(text) <= 10

    def _guess_chat_type(self, *, title: str, preview: str | None) -> str:
        if preview and ":" in preview:
            return "group"
        if "(" in title and ")" in title:
            return "group"
        return "direct"
