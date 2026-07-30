from __future__ import annotations

import logging
import time
from typing import Any

from ..core.errors import SessionNotFoundError
from .window_finder import WindowFinder

logger = logging.getLogger(__name__)


class Navigator:
    def __init__(self, window_finder: WindowFinder) -> None:
        self.window_finder = window_finder

    def switch_to_session(self, session_title: str) -> dict:
        window = self.window_finder.get_main_window()
        self._prepare_window(window)
        self._prepare_session_list(window)
        candidates = self.inspect_session_targets(session_title)
        session_item = candidates[0]["control"] if candidates else None
        if session_item is None:
            raise SessionNotFoundError(f"SESSION_NOT_FOUND: {session_title}")
        before_title = self._current_chat_title(window)
        strategies = []
        for name, action in (
            ("invoke", self._invoke_item),
            ("click_input", self._click_item),
            ("double_click_input", self._double_click_item),
            ("center_click", self._center_click_item),
            ("focus_then_enter", self._focus_then_enter),
        ):
            try:
                logger.info("attempting session switch strategy", extra={"session_title": session_title, "strategy": name})
                action(session_item)
                attempt_after_title = self._safe_current_title()
                strategies.append({"strategy": name, "ok": True, "after_title": attempt_after_title})
            except Exception as exc:
                logger.warning(
                    "session switch strategy failed",
                    extra={"session_title": session_title, "strategy": name, "error": str(exc)},
                )
                strategies.append({"strategy": name, "ok": False, "error": str(exc), "after_title": self._safe_current_title()})
            if self._wait_until_switched(session_title):
                after_title = self._current_chat_title(self.window_finder.get_main_window())
                logger.info(
                    "session switched",
                    extra={"session_title": session_title, "before_title": before_title, "after_title": after_title},
                )
                return {
                    "ok": True,
                    "status": "switched",
                    "summary": "会话切换成功",
                    "session_title": session_title,
                    "before_title": before_title,
                    "after_title": after_title,
                    "matched_candidates": [self._serialize_candidate(item) for item in candidates],
                    "strategies": strategies,
                }

        logger.warning(
            "session switch could not be confirmed",
            extra={"session_title": session_title, "before_title": before_title},
        )
        return {
            "ok": False,
            "status": "switch_failed",
            "summary": "已执行切换动作，但未确认标题变化",
            "session_title": session_title,
            "before_title": before_title,
            "after_title": self._current_chat_title(self.window_finder.get_main_window()),
            "matched_candidates": [self._serialize_candidate(item) for item in candidates],
            "strategies": strategies,
        }

    def inspect_session_targets(self, session_title: str) -> list[dict]:
        window = self.window_finder.get_main_window()
        session_list = self._find_by_auto_id(window, "session_list")
        if session_list is None:
            return []

        normalized_title = str(session_title or "").strip()
        matches: list[dict] = []
        for child in session_list.descendants():
            info = getattr(child, "element_info", None)
            auto_id = str(getattr(info, "automation_id", "") or "").strip()
            class_name = str(getattr(info, "class_name", "") or "").strip()
            if class_name != "mmui::ChatSessionCell" and not auto_id.startswith("session_item_"):
                continue
            text = self._read_text(child)
            title_from_auto_id = auto_id.removeprefix("session_item_") if auto_id.startswith("session_item_") else ""
            first_line = text.splitlines()[0].strip() if text.splitlines() else ""
            normalized_candidates = {value for value in (title_from_auto_id, first_line) if value}
            if normalized_title not in normalized_candidates:
                continue
            matches.append(
                {
                    "control": child,
                    "title_from_auto_id": title_from_auto_id or None,
                    "first_line": first_line or None,
                    "auto_id": auto_id or None,
                    "class_name": class_name or None,
                    "raw_text": text or None,
                    "rect": self._rect_payload(child),
                }
            )
        return matches

    def _find_session_item(self, window: Any, session_title: str):
        session_list = self._find_by_auto_id(window, "session_list")
        if session_list is None:
            return None
        normalized_title = str(session_title or "").strip()
        for child in session_list.descendants():
            info = getattr(child, "element_info", None)
            auto_id = str(getattr(info, "automation_id", "") or "").strip()
            class_name = str(getattr(info, "class_name", "") or "").strip()
            if class_name != "mmui::ChatSessionCell" and not auto_id.startswith("session_item_"):
                continue
            text = self._read_text(child)
            title_from_auto_id = auto_id.removeprefix("session_item_") if auto_id.startswith("session_item_") else ""
            first_line = text.splitlines()[0].strip() if text.splitlines() else ""
            if normalized_title in {title_from_auto_id, first_line}:
                return child
        return None

    def _prepare_window(self, window: Any) -> None:
        try:
            if hasattr(window, "restore"):
                window.restore()
        except Exception:
            pass
        try:
            window.set_focus()
        except Exception:
            pass
        try:
            if hasattr(window, "set_focus"):
                window.set_focus()
        except Exception:
            pass
        time.sleep(0.2)

    def _prepare_session_list(self, window: Any) -> None:
        session_list = self._find_by_auto_id(window, "session_list")
        if session_list is None:
            return
        try:
            session_list.set_focus()
        except Exception:
            pass
        time.sleep(0.2)

    def _find_by_auto_id(self, control: Any, auto_id: str):
        for child in control.descendants():
            info = getattr(child, "element_info", None)
            if str(getattr(info, "automation_id", "") or "").strip() == auto_id:
                return child
        return None

    def _current_chat_title(self, window: Any) -> str | None:
        for child in window.descendants():
            info = getattr(child, "element_info", None)
            automation_id = str(getattr(info, "automation_id", "") or "").strip()
            if automation_id.endswith("current_chat_name_label"):
                title = self._read_text(child)
                return title or None
        return None

    def _wait_until_switched(self, session_title: str, *, timeout_seconds: float = 4.0) -> bool:
        deadline = time.time() + timeout_seconds
        target = str(session_title or "").strip()
        while time.time() < deadline:
            try:
                current = self._current_chat_title(self.window_finder.get_main_window())
            except Exception:
                current = None
            if str(current or "").strip() == target:
                return True
            time.sleep(0.25)
        return False

    def _safe_current_title(self) -> str | None:
        try:
            return self._current_chat_title(self.window_finder.get_main_window())
        except Exception:
            return None

    def _invoke_item(self, control: Any) -> None:
        control.invoke()

    def _click_item(self, control: Any) -> None:
        control.click_input()

    def _double_click_item(self, control: Any) -> None:
        control.double_click_input()

    def _center_click_item(self, control: Any) -> None:
        from pywinauto.mouse import click

        rect = control.rectangle()
        center = ((int(rect.left) + int(rect.right)) // 2, (int(rect.top) + int(rect.bottom)) // 2)
        click(coords=center)

    def _focus_then_enter(self, control: Any) -> None:
        control.set_focus()
        time.sleep(0.1)
        try:
            control.type_keys("{ENTER}")
        except Exception:
            from pywinauto.keyboard import send_keys

            send_keys("{ENTER}")

    def _read_text(self, control: Any) -> str:
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
        if not parts:
            try:
                descendants = control.descendants()
            except Exception:
                descendants = []
            for child in descendants:
                try:
                    child_text = str(child.window_text() or "").strip()
                except Exception:
                    child_text = ""
                if child_text and child_text not in parts:
                    parts.append(child_text)
        return "\n".join(parts).strip()

    def _rect_payload(self, control: Any) -> dict | None:
        try:
            rect = control.rectangle()
        except Exception:
            return None
        return {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
        }

    def _serialize_candidate(self, item: dict) -> dict:
        return {
            "title_from_auto_id": item.get("title_from_auto_id"),
            "first_line": item.get("first_line"),
            "auto_id": item.get("auto_id"),
            "class_name": item.get("class_name"),
            "raw_text": item.get("raw_text"),
            "rect": item.get("rect"),
        }
