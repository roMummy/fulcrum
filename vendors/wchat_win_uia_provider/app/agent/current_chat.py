from __future__ import annotations

import hashlib
from typing import Any

from ..core.models import ConversationModel, MessageModel, RectModel
from .window_finder import WindowFinder


class CurrentChatCollector:
    def __init__(self, window_finder: WindowFinder) -> None:
        self.window_finder = window_finder

    def get_conversation(self) -> ConversationModel | None:
        window = self.window_finder.get_main_window()
        title_control = self._find_by_auto_id_suffix(window, "current_chat_name_label")
        title = self._read_text(title_control)
        if not title:
            return None
        count_control = self._find_by_auto_id_suffix(window, "current_chat_count_label")
        member_count_hint = self._read_text(count_control) or None
        chat_type = "group" if member_count_hint else "direct"
        return ConversationModel(
            conversation_id=self._build_conversation_id(title=title, chat_type=chat_type),
            title=title,
            chat_type=chat_type,
            member_count_hint=member_count_hint,
        )

    def get_messages(self) -> list:
        window = self.window_finder.get_main_window()
        message_list = self._find_by_auto_id(window, "chat_message_list")
        if message_list is None:
            return []
        conversation = self.get_conversation()
        chat_title = conversation.title if conversation else ""
        is_group = bool(conversation and conversation.chat_type == "group")
        items: list[MessageModel] = []
        seen_ids: set[str] = set()
        signature_counts: dict[tuple[str, str | None, str], int] = {}
        for child in message_list.descendants():
            info = getattr(child, "element_info", None)
            class_name = str(getattr(info, "class_name", "") or "").strip()
            auto_id = str(getattr(info, "automation_id", "") or "").strip()
            if class_name not in {"mmui::ChatTextItemView", "mmui::ChatBubbleItemView"} and "chat_bubble_item_view" not in auto_id:
                continue
            text = self._read_text(child)
            if not text:
                continue
            sender_name, normalized_content = self._extract_sender_and_content(text, is_group=is_group)
            signature = (class_name, sender_name, normalized_content)
            signature_counts[signature] = signature_counts.get(signature, 0) + 1
            message_id = self._build_message_id(
                chat_title=chat_title,
                class_name=class_name,
                auto_id=auto_id,
                text=normalized_content,
                occurrence=signature_counts[signature],
                sender_name=sender_name,
            )
            if message_id in seen_ids:
                continue
            seen_ids.add(message_id)
            items.append(
                MessageModel(
                    message_id=message_id,
                    message_type=self._classify_message(class_name, normalized_content),
                    content=normalized_content,
                    sender_name=sender_name,
                    direction="in" if sender_name else "unknown",
                    rect=self._rect_from_control(child),
                    raw_control_meta={
                        "class": class_name,
                        "auto_id": auto_id,
                        "source": "windows_uia",
                        "raw_text": text,
                    },
                )
            )
        return items

    def debug_message_list_tree(self, *, max_depth: int = 4, max_children: int = 20) -> dict:
        window = self.window_finder.get_main_window()
        message_list = self._find_by_auto_id(window, "chat_message_list")
        if message_list is None:
            return {"ok": False, "error": "CHAT_MESSAGE_LIST_NOT_FOUND"}
        return {
            "ok": True,
            "conversation": self.get_conversation().model_dump(mode="json") if self.get_conversation() else None,
            "tree": self._dump_control_tree(message_list, depth=0, max_depth=max_depth, max_children=max_children),
        }

    def debug_message_page_descendants(self, *, limit: int = 200) -> dict:
        window = self.window_finder.get_main_window()
        message_page = self._find_by_auto_id(window, "chat_message_page")
        if message_page is None:
            return {"ok": False, "error": "CHAT_MESSAGE_PAGE_NOT_FOUND"}
        items: list[dict] = []
        for child in message_page.descendants():
            info = getattr(child, "element_info", None)
            text = self._read_text(child)
            payload = {
                "name": self._safe_str(getattr(info, "name", None)),
                "class_name": self._safe_str(getattr(info, "class_name", None)),
                "auto_id": self._safe_str(getattr(info, "automation_id", None)),
                "control_type": self._safe_str(getattr(info, "control_type", None)),
                "framework_id": self._safe_str(getattr(info, "framework_id", None)),
                "text": text or None,
                "rect": self._rect_from_control(child).model_dump(mode="json") if self._rect_from_control(child) else None,
            }
            if any(payload.get(key) for key in ("name", "class_name", "auto_id", "text")):
                items.append(payload)
        return {
            "ok": True,
            "conversation": self.get_conversation().model_dump(mode="json") if self.get_conversation() else None,
            "items": items[: max(limit, 1)],
            "count": len(items),
        }

    def debug_message_contexts(self, *, limit: int = 20) -> dict:
        window = self.window_finder.get_main_window()
        message_list = self._find_by_auto_id(window, "chat_message_list")
        if message_list is None:
            return {"ok": False, "error": "CHAT_MESSAGE_LIST_NOT_FOUND"}
        items: list[dict] = []
        for child in message_list.descendants():
            info = getattr(child, "element_info", None)
            class_name = str(getattr(info, "class_name", "") or "").strip()
            auto_id = str(getattr(info, "automation_id", "") or "").strip()
            if class_name not in {"mmui::ChatTextItemView", "mmui::ChatBubbleItemView", "mmui::ChatBubbleReferItemView"} and "chat_bubble_item_view" not in auto_id:
                continue
            items.append(
                {
                    "message": self._control_payload(child),
                    "parent_chain": self._parent_chain_payload(child, max_depth=6),
                    "siblings": self._sibling_payloads(child, max_items=12),
                }
            )
        return {
            "ok": True,
            "conversation": self.get_conversation().model_dump(mode="json") if self.get_conversation() else None,
            "items": items[: max(limit, 1)],
            "count": len(items),
        }

    def _find_by_auto_id(self, control: Any, auto_id: str):
        for child in control.descendants():
            info = getattr(child, "element_info", None)
            if str(getattr(info, "automation_id", "") or "").strip() == auto_id:
                return child
        return None

    def _find_by_auto_id_suffix(self, control: Any, suffix: str):
        for child in control.descendants():
            info = getattr(child, "element_info", None)
            automation_id = str(getattr(info, "automation_id", "") or "").strip()
            if automation_id.endswith(suffix):
                return child
        return None

    def _read_text(self, control: Any) -> str:
        if control is None:
            return ""
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

    def _classify_message(self, class_name: str, text: str) -> str:
        lowered = str(text or "").strip().lower()
        if "文件" in text or any(lowered.endswith(ext) for ext in (".xls", ".xlsx", ".doc", ".docx", ".pdf", ".zip", ".rar")):
            return "file"
        if "[链接]" in text or "http://" in lowered or "https://" in lowered:
            return "link"
        if "加入群聊" in text or "撤回" in text:
            return "system"
        if class_name == "mmui::ChatTextItemView":
            return "text"
        return "unknown"

    def _extract_sender_and_content(self, text: str, *, is_group: bool) -> tuple[str | None, str]:
        normalized = str(text or "").strip()
        if not normalized:
            return None, ""
        if not is_group:
            return None, normalized
        lines = [line.strip() for line in normalized.splitlines() if line and line.strip()]
        if len(lines) < 2:
            return None, normalized
        first_line = lines[0]
        if not self._looks_like_sender_name(first_line):
            return None, normalized
        return first_line, "\n".join(lines[1:]).strip()

    def _looks_like_sender_name(self, value: str) -> bool:
        candidate = str(value or "").strip()
        if not candidate:
            return False
        if len(candidate) > 32:
            return False
        lowered = candidate.lower()
        if candidate.startswith("[") or candidate.startswith("http://") or candidate.startswith("https://"):
            return False
        if "引用 " in candidate or "加入群聊" in candidate or "撤回" in candidate:
            return False
        if any(lowered.endswith(ext) for ext in (".xls", ".xlsx", ".doc", ".docx", ".pdf", ".zip", ".rar", ".jpg", ".png")):
            return False
        return True

    def _rect_from_control(self, control: Any) -> RectModel | None:
        try:
            rect = control.rectangle()
        except Exception:
            return None
        return RectModel(left=int(rect.left), top=int(rect.top), right=int(rect.right), bottom=int(rect.bottom))

    def _dump_control_tree(self, control: Any, *, depth: int, max_depth: int, max_children: int) -> dict:
        info = getattr(control, "element_info", None)
        payload = self._control_payload(control)
        payload["depth"] = depth
        payload["children"] = []
        if depth >= max_depth:
            return payload
        try:
            children = control.children()
        except Exception:
            children = []
        for child in list(children)[: max(max_children, 1)]:
            payload["children"].append(
                self._dump_control_tree(child, depth=depth + 1, max_depth=max_depth, max_children=max_children)
            )
        payload["child_count"] = len(children)
        return payload

    def _safe_str(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = str(value).strip()
        return normalized or None

    def _control_payload(self, control: Any) -> dict:
        info = getattr(control, "element_info", None)
        rect = self._rect_from_control(control)
        return {
            "name": self._safe_str(getattr(info, "name", None)),
            "class_name": self._safe_str(getattr(info, "class_name", None)),
            "auto_id": self._safe_str(getattr(info, "automation_id", None)),
            "control_type": self._safe_str(getattr(info, "control_type", None)),
            "framework_id": self._safe_str(getattr(info, "framework_id", None)),
            "text": self._read_text(control) or None,
            "rect": rect.model_dump(mode="json") if rect else None,
        }

    def _parent_chain_payload(self, control: Any, *, max_depth: int) -> list[dict]:
        items: list[dict] = []
        current = control
        for _ in range(max_depth):
            try:
                current = current.parent()
            except Exception:
                current = None
            if current is None:
                break
            items.append(self._control_payload(current))
        return items

    def _sibling_payloads(self, control: Any, *, max_items: int) -> list[dict]:
        try:
            parent = control.parent()
        except Exception:
            parent = None
        if parent is None:
            return []
        try:
            siblings = parent.children()
        except Exception:
            siblings = []
        items: list[dict] = []
        for sibling in siblings[: max(max_items, 1)]:
            items.append(self._control_payload(sibling))
        return items

    def _rect_tuple(self, control: Any) -> tuple[int, int, int, int] | None:
        try:
            rect = control.rectangle()
        except Exception:
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)

    def _build_message_id(
        self,
        *,
        chat_title: str,
        class_name: str,
        auto_id: str,
        text: str,
        occurrence: int,
        sender_name: str | None,
    ) -> str:
        raw = "|".join(
            [
                chat_title,
                str(sender_name or ""),
                class_name,
                auto_id,
                text,
                str(occurrence),
            ]
        )
        return f"uia:msg:{hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]}"

    def _build_conversation_id(self, *, title: str, chat_type: str | None) -> str:
        normalized_type = str(chat_type or "").strip().lower()
        if normalized_type not in {"group", "direct"}:
            normalized_type = "unknown"
        return f"uia:{normalized_type}:{title}"
