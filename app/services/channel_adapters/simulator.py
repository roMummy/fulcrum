from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation

from .base import BaseChannelAdapter

logger = get_logger(__name__)


class SimulatorAdapter(BaseChannelAdapter):
    adapter_type = "simulator"

    def send_message(self, channel: Channel, conversation: Conversation, payload: dict[str, Any]) -> dict[str, Any]:
        message_type = str(payload.get("message_type") or "text").strip().lower() or "text"
        now = now_shanghai()
        logger.info(
            "simulator adapter loopback send",
            extra={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "message_type": message_type,
                "message_preview": str(payload.get("content") or "")[:120],
            },
        )
        raw_request = {
            "channel_id": channel.id,
            "channel_type": channel.channel_type,
            "conversation_id": conversation.id,
            "conversation_external_id": conversation.external_id,
            "payload": dict(payload),
        }
        raw_response = {
            "ok": True,
            "status": "accepted",
            "adapter_type": self.adapter_type,
            "simulated_at": now.isoformat(),
            "echo": {
                "message_type": message_type,
                "content": str(payload.get("content") or ""),
            },
        }
        return self.build_result(
            ok=True,
            conversation=conversation,
            message_type=message_type,
            transport_status="sent",
            status="sent",
            external_status="accepted",
            external_message_id=f"sim-{conversation.id}-{int(now.timestamp() * 1000)}",
            raw_request=raw_request,
            raw_response=raw_response,
            extra={"endpoint": "simulator://local-channel"},
        )

    def health_check(self, channel: Channel) -> dict[str, Any]:
        logger.debug("simulator adapter health check", extra={"channel_id": channel.id})
        return {
            "ok": True,
            "adapter_type": self.adapter_type,
            "status": "online",
            "summary": "内置模拟渠道可用",
            "details": {"channel_id": channel.id, "mode": "local-loopback"},
        }
