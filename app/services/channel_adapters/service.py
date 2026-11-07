from __future__ import annotations

import json
import re
from html import unescape
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.entities import Channel, Conversation, Message
from app.services.analytics import record_message_activity
from app.services.message_state import refresh_conversation_last_message_at

from .registry import get_channel_adapter

logger = get_logger(__name__)


def _build_outbound_sender(channel: Channel) -> tuple[str, str]:
    channel_type = str(channel.channel_type or "").strip().lower() or "channel"
    channel_id = int(getattr(channel, "id", 0) or 0)
    channel_name = str(getattr(channel, "name", "") or "").strip() or f"channel-{channel_id or 'unknown'}"
    return (
        f"outbound-adapter:{channel_type}:{channel_id}",
        f"{channel_type} · {channel_name} · #{channel_id}",
    )


def dispatch_outbound_message(
    db: Session,
    channel: Channel,
    conversation: Conversation,
    payload: dict[str, Any],
) -> tuple[Message, dict[str, Any]]:
    normalized_payload = _normalize_outbound_payload(channel, payload)
    logger.info(
        "dispatching outbound message",
        extra={
            "channel_id": channel.id,
            "channel_type": channel.channel_type,
            "conversation_id": conversation.id,
            "message_type": str(normalized_payload.get("message_type") or "text"),
        },
    )
    message_type = str(normalized_payload.get("message_type") or "text").strip().lower() or "text"
    content = str(normalized_payload.get("content") or "").strip()
    sender_id, sender_name = _build_outbound_sender(channel)
    message = Message(
        conversation_id=conversation.id,
        direction="out",
        sender_id=sender_id,
        sender_name=sender_name,
        content=content or f"[{message_type}]",
        message_type=message_type,
        status="queued",
        external_status="pending",
    )
    db.add(message)
    db.flush()

    adapter = get_channel_adapter(channel)
    adapter_result = adapter.send_message(channel, conversation, normalized_payload)
    _apply_adapter_result(message, adapter_result)
    record_message_activity(
        db,
        channel=channel,
        conversation=conversation,
        message=message,
    )
    refresh_conversation_last_message_at(db, conversation.id)
    db.commit()
    db.refresh(message)
    if not bool(adapter_result.get("ok")):
        logger.warning(
            "outbound message dispatched with failure",
            extra={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "message_id": message.id,
                "ok": False,
                "adapter_type": adapter_result.get("adapter_type"),
                "status": adapter_result.get("status"),
                "error_code": adapter_result.get("error_code"),
                "error_message": adapter_result.get("error_message"),
            },
        )
    return message, adapter_result


def dispatch_channel_action(
    channel: Channel,
    conversation: Conversation,
    *,
    action: str = "typing",
) -> dict[str, Any]:
    adapter = get_channel_adapter(channel)
    return adapter.send_chat_action(channel, conversation, action=action)


def _apply_adapter_result(message: Message, adapter_result: dict[str, Any]) -> None:
    message.external_message_id = _clean_string(adapter_result.get("external_message_id"))
    message.external_status = _clean_string(adapter_result.get("external_status")) or _clean_string(
        adapter_result.get("status")
    )
    message.status = "sent" if adapter_result.get("ok") else "failed"
    raw_request = adapter_result.get("raw_request")
    raw_response = adapter_result.get("raw_response")
    if raw_request:
        message.raw_request_json = json.dumps(raw_request, ensure_ascii=False)
    if raw_response:
        message.raw_response_json = json.dumps(raw_response, ensure_ascii=False)


def _clean_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _normalize_outbound_payload(channel: Channel, payload: dict[str, Any]) -> dict[str, Any]:
    normalized_payload = dict(payload)
    channel_type = str(channel.channel_type or "").strip().lower()
    message_type = str(normalized_payload.get("message_type") or "text").strip().lower() or "text"
    if channel_type == "telegram" and message_type == "file":
        xml = str(normalized_payload.get("xml") or "").strip()
        fallback_text = _build_xml_text_fallback(xml)
        if fallback_text:
            normalized_payload["message_type"] = "text"
            normalized_payload["content"] = fallback_text
            normalized_payload.setdefault("original_message_type", message_type)
    return normalized_payload


def _build_xml_text_fallback(xml: str) -> str:
    if not xml:
        return ""
    title = _extract_xml_tag(xml, "title")
    description = _extract_xml_tag(xml, "des")
    play_url = _extract_xml_tag(xml, "dataurl")
    page_url = _extract_xml_tag(xml, "url")
    lines: list[str] = []
    if title:
        lines.append(f"标题：{title}")
    if description:
        lines.append(description)
    if play_url:
        lines.append(f"播放链接：{play_url}")
    elif page_url:
        lines.append(f"链接：{page_url}")
    return "\n".join(lines).strip()


def _extract_xml_tag(xml: str, tag_name: str) -> str:
    pattern = rf"<{re.escape(tag_name)}>(.*?)</{re.escape(tag_name)}>"
    match = re.search(pattern, xml, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return unescape(str(match.group(1) or "").strip())
