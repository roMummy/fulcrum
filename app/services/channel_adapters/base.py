from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models.entities import Channel, Conversation


class BaseChannelAdapter(ABC):
    adapter_type = "generic"

    @abstractmethod
    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def send_chat_action(
        self,
        channel: Channel,
        conversation: Conversation,
        *,
        action: str = "typing",
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "adapter_type": self.adapter_type,
            "action": "send_chat_action",
            "status": "unsupported",
            "external_status": "unsupported",
        }

    def invite_group_members(
        self,
        channel: Channel,
        *,
        chatroom_external_id: str,
        member_external_ids: list[str],
    ) -> dict[str, Any]:
        """邀请成员加入群聊；不具备该能力的渠道必须明确返回不支持。"""

        return {
            "ok": False,
            "adapter_type": self.adapter_type,
            "action": "invite_group_members",
            "status": "unsupported",
            "external_status": "unsupported",
            "error_code": "group_invite_unsupported",
            "error_message": "当前渠道不支持邀请成员加入群聊",
        }

    def health_check(self, channel: Channel) -> dict[str, Any]:
        return {
            "ok": True,
            "adapter_type": self.adapter_type,
            "summary": "adapter health check not implemented",
        }

    def normalize_inbound_payload(
        self,
        channel: Channel,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        conversation_external_id = str(
            payload.get("conversation_id") or payload.get("room_id") or payload.get("chat_id") or "manual"
        )
        message_content = str(payload.get("content") or payload.get("text") or payload.get("message") or "").strip()
        sender_id = str(payload.get("sender_id") or "").strip() or None
        external_message_id = str(payload.get("message_id") or "").strip() or None
        event_key = str(payload.get("event_id") or external_message_id or "").strip() or None
        chat_type = "group" if payload.get("is_group") else "direct"

        return {
            "adapter_type": self.adapter_type,
            "event_key": event_key,
            "event_type": str(payload.get("event") or payload.get("event_type") or "message"),
            "should_persist_message": bool(message_content),
            "conversation": {
                "external_id": conversation_external_id,
                "title": payload.get("conversation_title") or payload.get("room_name"),
                "chat_type": chat_type,
            },
            "message": {
                "content": message_content,
                "sender_id": sender_id,
                "sender_name": payload.get("sender_name"),
                "message_type": str(payload.get("message_type") or "text"),
                "external_message_id": external_message_id,
            },
            "details": {
                "headers_used": sorted((headers or {}).keys()),
            },
        }

    def build_result(
        self,
        *,
        ok: bool,
        conversation: Conversation,
        message_type: str,
        transport_status: str,
        status: str,
        external_status: str | None = None,
        external_message_id: str | None = None,
        raw_request: dict[str, Any] | None = None,
        raw_response: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = {
            "ok": ok,
            "adapter_type": self.adapter_type,
            "action": "send_message",
            "message_type": message_type,
            "transport_status": transport_status,
            "status": status,
            "external_status": external_status or status,
            "conversation_external_id": conversation.external_id,
            "external_message_id": external_message_id,
            "raw_request": raw_request or {},
            "raw_response": raw_response or {},
        }
        if error_code:
            result["error_code"] = error_code
        if error_message:
            result["error_message"] = error_message
        if extra:
            result.update(extra)
        return result
