from __future__ import annotations

import hashlib
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from app.models.entities import Channel, Conversation

from .generic import GenericHttpAdapter


class WechatWinAutoAdapter(GenericHttpAdapter):
    adapter_type = "wechat_winauto"

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        normalized_payload = dict(payload)
        normalized_payload.setdefault("endpoint", "/send-message")
        normalized_payload.setdefault("receiver_id", self._resolve_receiver_id(conversation, payload))
        normalized_payload.setdefault("conversation_title", conversation.title or conversation.external_id)
        normalized_payload.setdefault("chat_type", conversation.chat_type or "direct")
        return super().send_message(channel, conversation, normalized_payload)

    def health_check(self, channel: Channel) -> dict[str, Any]:
        if not channel.api_base:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "offline",
                "summary": "缺少 API Base",
                "details": {"reason": "missing_api_base"},
            }

        adapter_config = self._load_adapter_config(channel)
        health_path = str(adapter_config.get("health_path") or "/health").strip() or "/health"
        timeout = self._resolve_timeout({}, adapter_config)
        target_url = self._build_absolute_url(channel.api_base, health_path)
        headers = self._build_headers(channel, {}, adapter_config)
        req = urlrequest.Request(target_url, method="GET")
        for key, value in headers.items():
            req.add_header(key, value)

        try:
            with urlrequest.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                parsed = self._parse_response_body(raw, getattr(response, "status", 200), target_url)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = self._parse_response_body(raw, exc.code, target_url)
        except URLError as exc:
            return {
                "ok": False,
                "adapter_type": self.adapter_type,
                "status": "offline",
                "summary": "Windows provider 不可达",
                "details": {
                    "reason": "provider_unreachable",
                    "error": str(exc.reason or exc),
                    "target_url": target_url,
                },
            }

        ok = self._response_indicates_success(parsed)
        return {
            "ok": ok,
            "adapter_type": self.adapter_type,
            "status": "online" if ok else "degraded",
            "summary": "Windows provider 在线" if ok else self._extract_error_message(parsed),
            "details": {
                "target_url": target_url,
                "response": parsed,
            },
        }

    def normalize_inbound_payload(
        self,
        channel: Channel,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        event_key = self._clean_string(payload.get("event_key")) or self._build_fallback_event_key(payload)
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}

        conversation_external_id = self._clean_string(
            conversation.get("external_id") or payload.get("conversation_id") or payload.get("chat_id")
        ) or "manual"
        conversation_title = self._clean_string(conversation.get("title") or payload.get("conversation_title"))
        chat_type = self._clean_string(conversation.get("chat_type") or payload.get("chat_type")) or "direct"

        content = self._clean_string(message.get("content") or payload.get("content")) or ""
        message_type = self._clean_string(message.get("message_type") or payload.get("message_type")) or "text"
        sender_id = self._clean_string(message.get("sender_id") or payload.get("sender_id"))
        sender_name = self._clean_string(message.get("sender_name") or payload.get("sender_name"))
        external_message_id = self._clean_string(message.get("external_message_id") or payload.get("message_id"))

        merged_details = {
            "headers_used": sorted((headers or {}).keys()),
            "provider_payload_type": self._clean_string(payload.get("provider_payload_type")),
            "provider_message_type": self._clean_string(payload.get("provider_message_type")),
            "raw_chat_name": self._clean_string(payload.get("chat_name")),
            "raw_sender": self._clean_string(payload.get("sender")),
            "raw_timestamp": payload.get("timestamp"),
        }
        merged_details.update({key: value for key, value in details.items() if value not in (None, "")})
        event_type = self._clean_string(payload.get("event_type") or payload.get("event")) or "message"
        if event_type == "message_attachment_ready":
            merged_details["skip_plugin_dispatch"] = True

        return {
            "adapter_type": self.adapter_type,
            "event_key": event_key,
            "event_type": event_type,
            "should_persist_message": bool(content) and event_type == "message",
            "conversation": {
                "external_id": conversation_external_id,
                "title": conversation_title,
                "chat_type": "group" if chat_type == "group" else "direct",
            },
            "message": {
                "content": content,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "message_type": message_type,
                "external_message_id": external_message_id,
            },
            "details": merged_details,
        }

    def _resolve_receiver_id(self, conversation: Conversation, payload: dict[str, Any]) -> str:
        return str(
            payload.get("receiver_id")
            or payload.get("external_id")
            or conversation.external_id
            or conversation.title
            or ""
        ).strip()

    def _build_absolute_url(self, base_url: str, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{str(base_url or '').rstrip('/')}{normalized_path}"

    def _build_fallback_event_key(self, payload: dict[str, Any]) -> str:
        raw_parts = [
            str(payload.get("conversation_id") or payload.get("chat_id") or payload.get("chat_name") or ""),
            str(payload.get("sender_id") or payload.get("sender") or ""),
            str(payload.get("content") or ""),
            str(payload.get("timestamp") or ""),
            str(payload.get("message_type") or payload.get("provider_message_type") or ""),
        ]
        digest = hashlib.sha1("|".join(raw_parts).encode("utf-8")).hexdigest()
        return digest

    def _clean_string(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = str(value).strip()
        return normalized or None
