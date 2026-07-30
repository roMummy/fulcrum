from __future__ import annotations

from .navigator import Navigator
from .window_finder import WindowFinder


class Sender:
    def __init__(self, window_finder: WindowFinder, navigator: Navigator) -> None:
        self.window_finder = window_finder
        self.navigator = navigator

    def send_text(self, session_title: str, content: str) -> dict:
        return {
            "ok": False,
            "status": "not_implemented",
            "summary": "尚未接入真实 UIA 文本发送能力",
            "session_title": session_title,
            "content": content,
        }

    def send_file(self, session_title: str, file_path: str) -> dict:
        return {
            "ok": False,
            "status": "not_implemented",
            "summary": "尚未接入真实 UIA 文件发送能力",
            "session_title": session_title,
            "file_path": file_path,
        }
