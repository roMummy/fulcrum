from __future__ import annotations

import json
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.entities import Channel, Conversation, CustomerOperationRequest, Message, WorkWechatContact
from app.services.task_queue import enqueue_job
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type


SUPPORTED_CONTENT_TYPES = {"text", "image", "file", "link_card", "card", "mini_program", "miniprogram"}


def submit_customer_operation(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    request_id = _first_text(raw, "request_id", "idempotency_key")
    channel_id = _coerce_int(raw.get("channel_id"))
    if not request_id:
        raise ValueError("request_id is required")
    if not channel_id:
        raise ValueError("channel_id is required")

    existing = db.scalar(
        select(CustomerOperationRequest).where(
            CustomerOperationRequest.channel_id == channel_id,
            CustomerOperationRequest.request_id == request_id,
        )
    )
    if existing is not None:
        return _format_operation_response(existing, duplicate=True)

    channel = db.get(Channel, channel_id)
    if channel is None:
        raise ValueError("channel_not_found")
    if channel.channel_type != "workwechat":
        raise ValueError("channel_must_be_workwechat")

    content = _normalize_content(raw.get("content"))
    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    target_contact_external_id = _first_text(raw, "contact_external_id", "target_contact_external_id") or _first_text(
        target,
        "contact_external_id",
        "external_id",
    )
    target_external_userid = _first_text(raw, "external_userid", "external_user_id") or _first_text(
        target,
        "external_userid",
        "external_user_id",
    )
    target_userid = _first_text(raw, "userid", "user_id") or _first_text(target, "userid", "user_id")

    operation = CustomerOperationRequest(
        channel_id=channel.id,
        request_id=request_id,
        source=_first_text(raw, "source") or _first_text(raw.get("biz_payload") if isinstance(raw.get("biz_payload"), dict) else {}, "source"),
        scene=_first_text(raw, "scene"),
        operation_type=_first_text(raw, "operation_type") or "send_message",
        content_type=content["message_type"],
        target_contact_external_id=target_contact_external_id or None,
        target_external_userid=target_external_userid or None,
        target_userid=target_userid or None,
        status="queued",
        content_json=_json_dump(content["source_content"]),
        biz_payload_json=_json_dump(raw.get("biz_payload") if isinstance(raw.get("biz_payload"), dict) else {}),
        raw_payload_json=_json_dump(raw),
    )
    db.add(operation)
    db.flush()

    if not channel.is_enabled:
        return _reject_operation(db, operation, "channel_disabled")

    contact = _resolve_contact(
        db,
        channel=channel,
        contact_external_id=target_contact_external_id,
        external_userid=target_external_userid,
        userid=target_userid,
    )
    if contact is None:
        return _reject_operation(db, operation, "contact_not_found")
    operation.contact_id = contact.id

    if contact.sync_status == "deleted":
        return _reject_operation(db, operation, "contact_deleted")

    conversation_external_id = str(contact.conversation_external_id or "").strip()
    if not conversation_external_id:
        return _reject_operation(db, operation, "contact_conversation_missing")

    conversation = _get_or_create_direct_conversation(db, channel=channel, contact=contact, external_id=conversation_external_id)
    operation.conversation_id = conversation.id

    outbound_payload = dict(content["outbound_payload"])
    outbound_payload.setdefault("external_id", conversation.external_id)
    outbound_payload.setdefault("receiver_id", conversation.external_id)
    outbound_payload.setdefault("chat_type", "direct")
    outbound_payload.setdefault("title", conversation.title or conversation.external_id)

    job = enqueue_job(
        db,
        job_type=resolve_outbound_job_type(channel),
        payload={
            "channel_id": channel.id,
            "conversation_id": conversation.id,
            "payload": outbound_payload,
            "plugin_code": "customer_operations",
            "customer_operation_request_id": operation.id,
        },
        priority=85,
        related_type="customer_operation_request",
        related_id=operation.id,
    )
    notify_cloud_job_enqueued(channel, job)
    operation.job_id = job.id
    operation.status = "queued"
    operation.status_reason = "queued"
    db.flush()
    return _format_operation_response(operation)


def mark_customer_operation_dispatched(
    db: Session,
    *,
    operation_id: int,
    message: Message | None,
    adapter_result: dict[str, Any],
) -> CustomerOperationRequest | None:
    operation = db.get(CustomerOperationRequest, operation_id)
    if operation is None:
        return None
    if message is not None:
        operation.message_id = message.id
    ok = bool(adapter_result.get("ok"))
    operation.status = "sent" if ok else "failed"
    operation.status_reason = str(
        adapter_result.get("error_message")
        or adapter_result.get("external_status")
        or adapter_result.get("status")
        or operation.status
    )[:255]
    operation.adapter_result_json = _json_dump(adapter_result)
    db.flush()
    return operation


def _resolve_contact(
    db: Session,
    *,
    channel: Channel,
    contact_external_id: str,
    external_userid: str,
    userid: str,
) -> WorkWechatContact | None:
    filters = []
    if contact_external_id:
        filters.append(WorkWechatContact.contact_external_id == contact_external_id)
    if external_userid:
        filters.append(WorkWechatContact.external_userid == external_userid)
        filters.append(WorkWechatContact.contact_external_id == external_userid)
    if userid:
        filters.append(WorkWechatContact.userid == userid)
        filters.append(WorkWechatContact.contact_external_id == userid)
    if not filters:
        return None
    return db.scalar(
        select(WorkWechatContact)
        .where(WorkWechatContact.channel_id == channel.id, or_(*filters))
        .order_by(WorkWechatContact.id.desc())
        .limit(1)
    )


def _get_or_create_direct_conversation(
    db: Session,
    *,
    channel: Channel,
    contact: WorkWechatContact,
    external_id: str,
) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.bot_id == channel.bot_id,
            Conversation.channel_id == channel.id,
            Conversation.external_id == external_id,
        )
    )
    if conversation is not None:
        return conversation
    title = contact.display_name or contact.name or contact.alias or contact.contact_external_id
    conversation = Conversation(
        bot_id=channel.bot_id,
        channel_id=channel.id,
        channel_type=channel.channel_type,
        external_id=external_id,
        title=title,
        chat_type="direct",
    )
    db.add(conversation)
    db.flush()
    return conversation


def _normalize_content(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        content_obj = {"type": "text", "text": content}
    elif isinstance(content, dict):
        content_obj = dict(content)
    else:
        raise ValueError("content must be object or string")

    content_type = str(content_obj.get("type") or content_obj.get("message_type") or "text").strip().lower() or "text"
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise ValueError(f"unsupported_content_type:{content_type}")
    if content_type == "card":
        content_type = "link_card"
    if content_type == "miniprogram":
        content_type = "mini_program"

    outbound: dict[str, Any] = {"message_type": content_type}
    if content_type == "text":
        text = _first_text(content_obj, "text", "content", "message")
        if not text:
            raise ValueError("text content is required")
        outbound["content"] = text
    elif content_type == "image":
        _copy_keys(content_obj, outbound, ("image_url", "image_path", "image_base64", "file_url", "file_path", "file_name", "base64"))
        outbound["content"] = _first_text(content_obj, "text", "content", "caption") or "[image]"
        if not any(outbound.get(key) for key in ("image_url", "image_path", "image_base64", "file_url", "file_path", "base64")):
            raise ValueError("image payload requires image_url/image_path/image_base64")
    elif content_type == "file":
        _copy_keys(content_obj, outbound, ("file_url", "file_path", "file_base64", "file_name", "download_url", "base64"))
        outbound["content"] = _first_text(content_obj, "text", "content", "file_name") or "[file]"
        if not any(outbound.get(key) for key in ("file_url", "file_path", "file_base64", "download_url", "base64")):
            raise ValueError("file payload requires file_url/file_path/file_base64")
    elif content_type == "link_card":
        _copy_keys(content_obj, outbound, ("url", "image_url", "title", "desc"))
        outbound["content"] = _first_text(content_obj, "title") or "[link_card]"
        if not outbound.get("url") or not outbound.get("title"):
            raise ValueError("link_card requires url and title")
    elif content_type == "mini_program":
        _copy_keys(
            content_obj,
            outbound,
            ("username", "appid", "appname", "appicon", "title", "page_path", "pagepath", "file_id", "aes_key", "md5", "size"),
        )
        if outbound.get("pagepath") and not outbound.get("page_path"):
            outbound["page_path"] = outbound["pagepath"]
        outbound["content"] = _first_text(content_obj, "title") or "[mini_program]"
        required = ("username", "appid", "appname", "appicon", "title", "page_path", "file_id", "aes_key", "md5", "size")
        missing = [key for key in required if outbound.get(key) in (None, "")]
        if missing:
            raise ValueError(f"mini_program missing fields:{','.join(missing)}")

    return {
        "message_type": content_type,
        "source_content": content_obj,
        "outbound_payload": outbound,
    }


def _reject_operation(db: Session, operation: CustomerOperationRequest, reason: str) -> dict[str, Any]:
    operation.status = "rejected"
    operation.status_reason = reason
    db.flush()
    return _format_operation_response(operation)


def _format_operation_response(operation: CustomerOperationRequest, *, duplicate: bool = False) -> dict[str, Any]:
    return {
        "ok": operation.status not in {"rejected", "failed"},
        "duplicate": duplicate,
        "request_id": operation.request_id,
        "operation_id": operation.id,
        "channel_id": operation.channel_id,
        "contact_id": operation.contact_id,
        "conversation_id": operation.conversation_id,
        "job_id": operation.job_id,
        "message_id": operation.message_id,
        "status": operation.status,
        "status_reason": operation.status_reason,
        "content_type": operation.content_type,
    }


def _copy_keys(source: dict[str, Any], target: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            target[key] = value


def _first_text(source: Any, *keys: str) -> str:
    if not isinstance(source, dict):
        return ""
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            text = str(value).strip()
            if text:
                return text
    return ""


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)
