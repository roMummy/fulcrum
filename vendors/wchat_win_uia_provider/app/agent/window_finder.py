from __future__ import annotations

import time

from ..core.config import settings
from ..core.errors import WechatWindowNotFoundError


class WindowFinder:
    def __init__(self) -> None:
        self._last_error: str | None = None

    def is_window_available(self) -> bool:
        try:
            self.get_main_window()
        except Exception as exc:
            self._last_error = str(exc)
            return False
        return True

    def get_main_window(self):
        last_error: Exception | None = None
        desktop = self._get_desktop()
        for _ in range(max(int(settings.reconnect_attempts), 1)):
            try:
                window = self._find_main_window_once(desktop)
                self._last_error = None
                return window
            except Exception as exc:
                last_error = exc
                time.sleep(max(float(settings.reconnect_delay_seconds), 0.1))
        raise WechatWindowNotFoundError(str(last_error) if last_error else "WECHAT_WINDOW_NOT_FOUND")

    def get_last_error(self) -> str | None:
        return self._last_error

    def describe_target(self) -> dict:
        return {
            "title": settings.main_window_title,
            "class_name": settings.main_window_class_name,
        }

    def _get_desktop(self):
        from pywinauto import Desktop

        return Desktop(backend="uia")

    def _find_main_window_once(self, desktop):
        windows = desktop.windows()
        target_class = str(settings.main_window_class_name or "").strip()
        target_title = str(settings.main_window_title or "").strip()
        for window in windows:
            info = getattr(window, "element_info", None)
            class_name = str(getattr(info, "class_name", "") or "").strip()
            title = str(getattr(info, "name", "") or "").strip() or str(window.window_text() or "").strip()
            if target_class and class_name == target_class:
                return window
            if target_title and title == target_title and (not target_class or class_name == target_class):
                return window
        raise WechatWindowNotFoundError("WECHAT_WINDOW_NOT_FOUND")
