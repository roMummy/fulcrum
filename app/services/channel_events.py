from __future__ import annotations

import json
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import SHANGHAI_TZ, ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, Message, MessageAttachment, RawWebhookEvent, TaskJob
from app.services.analytics import record_message_activity
from app.services.channel_adapters import get_channel_adapter
from app.services.inbound_media import create_wechat_media_attachment
from app.services.message_state import refresh_conversation_last_message_at
from app.services.open_webhooks import dispatch_open_message_event
from app.services.chatroom_registry import is_collection_off
from app.services.task_queue import INBOUND_MEDIA_FETCH_JOB, PLUGIN_DISPATCH_JOB, WORKWECHAT_CLOUD_CONTROL_JOB, enqueue_job
from app.services.workwechat_cloud_poll import is_cloud_poll_enabled, notify_cloud_job_enqueued
from app.services.wechat_ipad.cache import has_chatroom as wechat_cache_has_chatroom
from app.services.wechat_ipad.cache import has_member as wechat_cache_has_member

logger = get_logger(__name__)
BEIJING_TZ = SHANGHAI_TZ
QUIET_HOURS_START = dt_time(hour=0, minute=30)
QUIET_HOURS_END = dt_time(hour=5, minute=0)


def ingest_inbound_event(
    db: Session,
    channel: Channel,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    logger.info(
        "ingesting inbound event",
        extra={"channel_id": channel.id, "channel_type": channel.channel_type, "source": source or channel.channel_type},
    )
    adapter = get_channel_adapter(channel)
    normalized = adapter.normalize_inbound_payload(channel, payload, headers=headers or {})
    return ingest_normalized_event(
        db,
        channel,
        normalized,
        source=source,
        raw_payload=payload,
    )


def _group_collection_off(db: Session, channel: Channel, normalized: dict[str, Any]) -> bool:
    """入站事件所在的群是否被设为“完全关闭”。"""

    conversation = normalized.get("conversation")
    if not isinstance(conversation, dict) or str(conversation.get("chat_type") or "") != "group":
        return False
    external_id = str(conversation.get("external_id") or "").strip()
    if not external_id:
        return False
    room = db.scalar(select(Chatroom).where(Chatroom.channel_id == channel.id, Chatroom.external_id == external_id))
    return is_collection_off(room)


def ingest_normalized_event(
    db: Session,
    channel: Channel,
    normalized: dict[str, Any],
    *,
    source: str | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_key = str(normalized.get("event_key") or "").strip()
    # 已逻辑删除的账号不再接收任何事件。
    if channel.deleted_at is not None:
        return {"ok": True, "ignored": True, "reason": "channel_deleted", "raw_event_id": None, "mapped": False, "normalized": normalized}
    # 群设为“完全关闭”时不接收、不记录:不落原始事件、会话和消息,也不入队调度。
    if _group_collection_off(db, channel, normalized):
        logger.debug("inbound event ignored because group collection is off", extra={"channel_id": channel.id})
        return {"ok": True, "ignored": True, "reason": "group_collection_off", "raw_event_id": None, "mapped": False, "normalized": normalized}
    logger.debug(
        "ingesting normalized event",
        extra={"channel_id": channel.id, "channel_type": channel.channel_type, "event_key": event_key or "-"},
    )

    existing = None
    if event_key:
        existing = db.scalar(
            select(RawWebhookEvent).where(RawWebhookEvent.channel_id == channel.id, RawWebhookEvent.event_key == event_key)
        )
    if existing:
        logger.info(
            "deduplicated inbound event",
            extra={"channel_id": channel.id, "raw_event_id": existing.id, "event_key": event_key or "-"},
        )
        return {
            "ok": True,
            "deduplicated": True,
            "raw_event_id": existing.id,
            "mapped": existing.status in {"mapped", "dispatched"},
            "normalized": normalized,
        }

    raw_event = RawWebhookEvent(
        channel_id=channel.id,
        source=(source or channel.channel_type).strip() or channel.channel_type,
        event_key=event_key or None,
        payload_json=json.dumps(raw_payload if isinstance(raw_payload, dict) else normalized, ensure_ascii=False),
        status="received",
    )
    db.add(raw_event)
    db.flush()
    logger.info(
        "raw inbound event persisted",
        extra={"channel_id": channel.id, "raw_event_id": raw_event.id, "event_key": event_key or "-"},
    )

    conversation_payload = normalized.get("conversation") or {}
    message_payload = normalized.get("message") or {}
    conversation_external_id = str(conversation_payload.get("external_id") or "manual")
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.bot_id == channel.bot_id,
            Conversation.channel_id == channel.id,
            Conversation.external_id == conversation_external_id,
        )
    )
    if not conversation:
        conversation = Conversation(
            bot_id=channel.bot_id,
            channel_id=channel.id,
            channel_type=channel.channel_type,
            external_id=conversation_external_id,
            title=conversation_payload.get("title"),
            chat_type=str(conversation_payload.get("chat_type") or "direct"),
        )
        db.add(conversation)
        db.flush()
    elif _should_update_conversation_title(conversation.title, conversation_payload.get("title"), conversation_external_id):
        # 企微群名称可能在首条消息之后才通过 Hook 或群列表同步补齐；
        # 旧值为空或只是 external_id 时允许被真实群名覆盖，避免后台一直显示 R:xxx。
        conversation.title = str(conversation_payload.get("title")).strip()

    if channel.channel_type == "wechat_gugatool":
        # 咕嘎助手没有群列表接口，群实体只能在收到群消息时按名册落库；
        # 这样群管理页才能在群第一次说话后就有群和成员，而不是一直空白。
        from app.services.wechat_gugatool_chatrooms import sync_wechat_gugatool_chatroom_from_message

        sync_wechat_gugatool_chatroom_from_message(db, channel, normalized, conversation=conversation)

    if channel.channel_type == "wechat_ipad":
        from app.services.wechat_ipad.chatrooms import sync_wechat_ipad_chatroom_from_message
        from app.services.wechat_ipad.service import should_sync_wechat_ipad_chatroom_for_event, sync_wechat_ipad_chatrooms

        conversation_payload = normalized.get("conversation") or {}
        message_payload = normalized.get("message") or {}
        chatroom_external_id = str(conversation_payload.get("external_id") or "").strip()
        member_external_id = str(message_payload.get("sender_id") or "").strip()
        cache_chatroom_known = (
            str(conversation_payload.get("chat_type") or "") == "group"
            and bool(chatroom_external_id)
            and wechat_cache_has_chatroom(channel.id, chatroom_external_id)
        )
        cache_member_known = (
            bool(chatroom_external_id)
            and bool(member_external_id)
            and wechat_cache_has_member(channel.id, chatroom_external_id, member_external_id)
        )
        logger.info(
            "wechat ipad entity cache inspected",
            extra={
                "channel_id": channel.id,
                "chatroom_id": chatroom_external_id,
                "member_external_id": member_external_id,
                "cache_chatroom_known": cache_chatroom_known,
                "cache_member_known": cache_member_known,
            },
        )
        materialize_result = sync_wechat_ipad_chatroom_from_message(
            db,
            channel,
            normalized,
            conversation=conversation,
        )
        logger.info(
            "wechat ipad entity cache materialized",
            extra={
                "channel_id": channel.id,
                "chatroom_id": materialize_result.get("chatroom_id"),
                "member_external_id": materialize_result.get("member_external_id"),
                "chatroom_created": bool(materialize_result.get("chatroom_created")),
                "member_created": bool(materialize_result.get("member_created")),
            },
        )
        chatroom_sync_signal = should_sync_wechat_ipad_chatroom_for_event(
            db,
            channel,
            normalized,
            entity_state={
                "cache_chatroom_known": cache_chatroom_known,
                "cache_member_known": cache_member_known,
                "chatroom_created": bool(materialize_result.get("chatroom_created")),
                "member_created": bool(materialize_result.get("member_created")),
            },
        )
        if chatroom_sync_signal:
            logger.info(
                "wechat ipad inbound event triggering chatroom sync",
                extra={
                    "channel_id": channel.id,
                    "chatroom_id": chatroom_sync_signal.get("chatroom_id"),
                    "chatroom_ids": chatroom_sync_signal.get("chatroom_ids") or [],
                    "reason": chatroom_sync_signal.get("reason"),
                    "event_key": chatroom_sync_signal.get("event_key"),
                },
            )
            sync_wechat_ipad_chatrooms(
                db,
                channel,
                chatroom_ids=list(chatroom_sync_signal.get("chatroom_ids") or [chatroom_sync_signal["chatroom_id"]]),
            )
    elif channel.channel_type == "wechat_winauto":
        from app.services.wechat_winauto_chatrooms import sync_wechat_winauto_chatroom_from_message

        sync_wechat_winauto_chatroom_from_message(
            db,
            channel,
            normalized,
            conversation=conversation,
        )
    elif channel.channel_type == "wechat_webridge":
        # 微桥的群记录只由 groups_all 同步创建:未登记的群有人说话、入群事件、"邀请你加入群聊"都触发一次(去重防抖的)群同步。
        from app.services.wechat_webridge.group_sync import observe_inbound_event

        observe_inbound_event(db, channel, normalized, conversation)
    elif channel.channel_type == "workwechat":
        from app.services.workwechat_chatrooms import sync_workwechat_chatroom_from_message
        from app.services.workwechat_contacts import apply_workwechat_contact_event

        # workwechat 使用独立 Hook 协议；群和成员先由消息被动观测落库，主动成员快照由群详情页同步按钮补齐。
        apply_workwechat_contact_event(db, channel, normalized)
        sync_workwechat_chatroom_from_message(
            db,
            channel,
            normalized,
            conversation=conversation,
        )

    message_content = str(message_payload.get("content") or "")
    message_id: int | None = None
    mapped = bool(normalized.get("should_persist_message") and message_content)
    message_direction = _resolve_message_direction(normalized)
    if mapped:
        message = Message(
            conversation_id=conversation.id,
            direction=message_direction,
            sender_id=str(message_payload.get("sender_id") or "") or None,
            sender_name=message_payload.get("sender_name"),
            content=message_content,
            message_type=str(message_payload.get("message_type") or "text"),
            external_message_id=str(message_payload.get("external_message_id") or "") or None,
            status="received",
        )
        db.add(message)
        _update_chatroom_member_last_active(
            db,
            channel=channel,
            conversation=conversation,
            normalized=normalized,
        )
        raw_event.status = "mapped"
        logger.info(
            "inbound message mapped",
            extra={
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                # 中文业务日志:群/会话名、发言人、内容摘要,运维不用再对 ID 查库。
                "conversation_title": conversation.title or conversation.external_id,
                "sender_name": message_payload.get("sender_name") or message_payload.get("sender_id") or "-",
                "message_preview": message_content[:120].replace("\n", " "),
            },
        )
    else:
        raw_event.status = "ignored"
        logger.info(
            "inbound event ignored",
            extra={"channel_id": channel.id, "raw_event_id": raw_event.id, "event_key": event_key or "-"},
        )

    db.flush()
    if mapped and "message" in locals():
        message_id = message.id
        record_message_activity(
            db,
            channel=channel,
            conversation=conversation,
            message=message,
        )
        refresh_conversation_last_message_at(db, conversation.id)
        _queue_inbound_media_fetch_if_needed(
            db,
            channel=channel,
            conversation=conversation,
            raw_event=raw_event,
            normalized=normalized,
            message=message,
        )
        _materialize_wechat_winauto_attachment_if_needed(
            db,
            channel=channel,
            conversation=conversation,
            raw_event=raw_event,
            normalized=normalized,
            message=message,
        )

    _apply_wechat_winauto_attachment_ready_if_needed(
        db,
        channel=channel,
        conversation=conversation,
        raw_event=raw_event,
        normalized=normalized,
    )
    _attach_message_attachments_to_normalized_event(
        db,
        channel=channel,
        raw_event=raw_event,
        normalized=normalized,
        message_id=message_id,
    )
    if mapped and "message" in locals():
        ready_attachments = _load_ready_message_attachments(db, channel=channel, message=message)
        if message.direction == "in":
            # message.received 只覆盖真正入站消息；客服自己发出的 out 留痕不应通知业务系统重复处理。
            dispatch_open_message_event(
                db,
                event_type="message.received",
                channel=channel,
                conversation=conversation,
                message=message,
                attachments=ready_attachments,
            )
            # file.received 是开放协议里的“文件已就绪”事件，只应承载普通文件附件。
            # 视频/图片虽然也会落成 MessageAttachment，但不能用 file.received 通知收发单系统，
            # 否则外部系统容易把视频缓存误当成 Excel 回单继续解析。
            file_attachments = [
                attachment
                for attachment in ready_attachments
                if str(getattr(attachment, "attachment_type", "") or "").strip().lower() == "file"
            ]
            if file_attachments:
                dispatch_open_message_event(
                    db,
                    event_type="file.received",
                    channel=channel,
                    conversation=conversation,
                    message=message,
                    attachments=file_attachments,
                )

    quiet_hours_state = _resolve_quiet_hours_state(normalized)
    details = normalized.get("details") if isinstance(normalized.get("details"), dict) else {}
    if bool(details.get("skip_plugin_dispatch")):
        dispatch_payload = {
            "mode": "storage_only",
            "queued": False,
            "job_id": None,
            "job_type": None,
            "raw_event_id": raw_event.id,
            "conversation_id": conversation.id,
            "message_id": message_id,
            "status": "skipped",
            "reason": "skip_plugin_dispatch",
        }
        raw_event.dispatch_summary_json = json.dumps(dispatch_payload, ensure_ascii=False)
        db.commit()
        logger.info(
            "normalized event skipped plugin dispatch by explicit flag",
            extra={
                "channel_id": channel.id,
                "raw_event_id": raw_event.id,
                "conversation_id": conversation.id,
                "message_id": message_id or 0,
                "mapped": mapped,
                "event_type": normalized.get("event_type"),
            },
        )
        return {
            "ok": True,
            "deduplicated": False,
            "raw_event_id": raw_event.id,
            "mapped": mapped,
            "normalized": normalized,
            "dispatch": dispatch_payload,
        }
    if quiet_hours_state["skip_plugin_dispatch"]:
        dispatch_payload = {
            "mode": "quiet_hours_storage_only",
            "queued": False,
            "job_id": None,
            "job_type": None,
            "raw_event_id": raw_event.id,
            "conversation_id": conversation.id,
            "message_id": message_id,
            "status": "skipped",
            "reason": "quiet_hours",
            "event_time": quiet_hours_state["event_time_iso"],
            "event_time_display": quiet_hours_state["event_time_display"],
        }
        raw_event.dispatch_summary_json = json.dumps(dispatch_payload, ensure_ascii=False)
        db.commit()
        logger.info(
            "normalized event skipped plugin dispatch by quiet hours",
            extra={
                "channel_id": channel.id,
                "raw_event_id": raw_event.id,
                "conversation_id": conversation.id,
                "message_id": message_id or 0,
                "mapped": mapped,
                "event_time": quiet_hours_state["event_time_display"],
            },
        )
        return {
            "ok": True,
            "deduplicated": False,
            "raw_event_id": raw_event.id,
            "mapped": mapped,
            "normalized": normalized,
            "dispatch": dispatch_payload,
        }

    queue_payload = {
        "channel_id": channel.id,
        "raw_event_id": raw_event.id,
        "conversation_id": conversation.id,
        "message_id": message_id,
        "normalized_event": normalized,
    }
    job = enqueue_job(
        db,
        job_type=PLUGIN_DISPATCH_JOB,
        payload=queue_payload,
        priority=100,
        related_type="raw_webhook_event",
        related_id=raw_event.id,
    )
    dispatch_payload = {
        "mode": "async",
        "queued": True,
        "job_id": job.id,
        "job_type": job.job_type,
        "raw_event_id": raw_event.id,
        "conversation_id": conversation.id,
        "message_id": message_id,
        "status": "pending",
    }
    raw_event.dispatch_summary_json = json.dumps(dispatch_payload, ensure_ascii=False)
    db.commit()
    logger.info(
        "normalized event queued for async dispatch",
        extra={
            "channel_id": channel.id,
            "raw_event_id": raw_event.id,
            "conversation_id": conversation.id,
            "message_id": message_id or 0,
            "mapped": mapped,
            "job_id": job.id,
        },
    )
    return {
        "ok": True,
        "deduplicated": False,
        "raw_event_id": raw_event.id,
        "mapped": mapped,
        "normalized": normalized,
        "dispatch": dispatch_payload,
    }


def _resolve_message_direction(normalized: dict[str, Any]) -> str:
    details = normalized.get("details") if isinstance(normalized.get("details"), dict) else {}
    message_payload = normalized.get("message") if isinstance(normalized.get("message"), dict) else {}
    candidate = str(message_payload.get("direction") or details.get("direction") or "").strip().lower()
    # 当前只允许 in/out 两种方向落库，避免外部 payload 写入任意状态值污染统计口径。
    return candidate if candidate in {"in", "out"} else "in"


def _resolve_wechat_winauto_attachment_direction(
    normalized: dict[str, Any],
    *,
    message: Message | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    """解析企微附件方向。

    Provider 会把自己发出的文件也通过 Hook 回显给 Fulcrum，这类事件的附件不是供应商回传文件；
    附件方向必须跟随消息/明细里的 out 标记，否则后台会把出站回显展示成入站下载失败。
    """

    detail_payload = details if isinstance(details, dict) else {}
    if message is not None and str(message.direction or "").strip().lower() in {"in", "out"}:
        return str(message.direction or "").strip().lower()
    candidate = str(detail_payload.get("direction") or "").strip().lower()
    if candidate in {"in", "out"}:
        return candidate
    return _resolve_message_direction(normalized)


def _queue_inbound_media_fetch_if_needed(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
    raw_event: RawWebhookEvent,
    normalized: dict[str, Any],
    message: Message,
) -> None:
    if channel.channel_type not in {"wechat_ipad", "wechat_webridge", "wechat_gugatool"}:
        return
    attachment_type = str(message.message_type or "").strip()
    if attachment_type not in {"image", "voice", "video"}:
        return
    # 手机端下载媒体很耗资源(上传一次最长可达数十秒并占用手机串行通道,会拖慢消息上报),
    # 因此 iPad 与微桥统一按“群自动下载附件”开关控制,默认关闭;私聊不下载。
    if not _chatroom_auto_download_media_enabled(db, channel=channel, conversation=conversation):
        return
    details = normalized.get("details")
    if not isinstance(details, dict):
        return
    metadata_key = {
        "image": "image_info",
        "voice": "voice_info",
        "video": "video_info",
    }.get(attachment_type, "")
    media_info = details.get(metadata_key)
    if channel.channel_type in {"wechat_webridge", "wechat_gugatool"}:
        media_info = details.get("media") or {}
    if not isinstance(media_info, dict):
        return
    attachment = create_wechat_media_attachment(
        db,
        channel=channel,
        raw_event_id=raw_event.id,
        conversation_id=conversation.id,
        message_id=message.id,
        attachment_type=attachment_type,
        metadata=media_info,
        provider=channel.channel_type,
    )
    if channel.channel_type == "wechat_webridge":
        from app.services.wechat_webridge.service import build_upload_url, enqueue_media_upload_command

        upload_url = build_upload_url(channel, attachment.id)
        enqueue_media_upload_command(
            db,
            channel=channel,
            conversation=conversation,
            message_id=str(message.external_message_id or message.id),
            media_type=attachment_type,
            upload_url=upload_url,
            attachment_id=attachment.id,
        )
    else:
        enqueue_job(
            db,
            job_type=INBOUND_MEDIA_FETCH_JOB,
            payload={"attachment_id": attachment.id, "channel_id": channel.id},
            priority=95,
            related_type="message_attachment",
            related_id=attachment.id,
        )


def _chatroom_auto_download_media_enabled(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
) -> bool:
    if str(conversation.chat_type or "").strip() != "group":
        return False
    group = db.scalar(
        select(Chatroom).where(
            Chatroom.channel_id == channel.id,
            Chatroom.external_id == conversation.external_id,
        )
    )
    if group is None:
        return False
    # 群功能「自动下载附件」:群已启用且该能力已打开才下载。
    from app.services.group_features.store import is_enabled

    return is_enabled(group, "media_download")


def _update_chatroom_member_last_active(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
    normalized: dict[str, Any],
) -> None:
    conversation_payload = normalized.get("conversation") or {}
    if str(conversation_payload.get("chat_type") or "").strip() != "group":
        return
    group_external_id = str(conversation_payload.get("external_id") or conversation.external_id or "").strip()
    if not group_external_id:
        return
    message_payload = normalized.get("message") or {}
    member_external_id = str(message_payload.get("sender_id") or "").strip()
    if not member_external_id:
        return
    event_time = _resolve_event_time(normalized)
    member = db.scalar(
        select(ChatroomMember).where(
            ChatroomMember.channel_id == channel.id,
            ChatroomMember.member_external_id == member_external_id,
            ChatroomMember.chatroom_id.in_(select_chatroom_member_chatroom_ids(channel.id, group_external_id)),
        )
    )
    if member is None:
        return
    member.status = "active"
    member.last_active_at = event_time


def select_chatroom_member_chatroom_ids(channel_id: int, external_id: str):
    from app.models.entities import Chatroom

    return select(Chatroom.id).where(Chatroom.channel_id == channel_id, Chatroom.external_id == external_id)


def _should_update_conversation_title(current_title: Any, incoming_title: Any, external_id: str) -> bool:
    incoming = str(incoming_title or "").strip()
    if not incoming:
        return False
    current = str(current_title or "").strip()
    external = str(external_id or "").strip()
    if not current:
        return True
    # 当前标题如果只是技术 ID，不算人工可读群名；后续拿到真实群名时应该更新。
    return bool(external and current == external and incoming != external)


def _resolve_quiet_hours_state(normalized: dict[str, Any]) -> dict[str, Any]:
    event_time = _resolve_event_time(normalized)
    local_time = event_time.astimezone(BEIJING_TZ)
    local_clock = local_time.time().replace(tzinfo=None)
    in_quiet_hours = QUIET_HOURS_START <= local_clock < QUIET_HOURS_END
    return {
        "skip_plugin_dispatch": in_quiet_hours,
        "event_time_iso": local_time.isoformat(),
        "event_time_display": local_time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _resolve_event_time(normalized: dict[str, Any]) -> datetime:
    details = normalized.get("details")
    if isinstance(details, dict):
        timestamp_value = details.get("timestamp")
        parsed_timestamp = _parse_timestamp_value(timestamp_value)
        if parsed_timestamp is not None:
            return parsed_timestamp
    return now_shanghai()


def _parse_timestamp_value(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return ensure_shanghai_datetime(value)
    try:
        if isinstance(value, str) and any(marker in value for marker in ("T", "-", ":")):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return ensure_shanghai_datetime(parsed)
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    if normalized > 10_000_000_000:
        normalized = normalized / 1000
    try:
        return datetime.fromtimestamp(normalized, tz=SHANGHAI_TZ)
    except (OverflowError, OSError, ValueError):
        return None


def _materialize_wechat_winauto_attachment_if_needed(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
    raw_event: RawWebhookEvent,
    normalized: dict[str, Any],
    message: Message,
) -> None:
    if channel.channel_type not in {"wechat_winauto", "workwechat"}:
        return
    attachment_type = str(message.message_type or "").strip()
    if attachment_type not in {"image", "file", "video"}:
        return
    details = normalized.get("details")
    if not isinstance(details, dict):
        return
    attachment_direction = _resolve_wechat_winauto_attachment_direction(normalized, message=message, details=details)
    attachment = db.scalar(
        select(MessageAttachment).where(
            MessageAttachment.raw_event_id == raw_event.id,
            MessageAttachment.channel_id == channel.id,
            MessageAttachment.attachment_type == attachment_type,
            MessageAttachment.provider.in_(("wechat_winauto", "workwechat")),
        )
    )
    if not attachment:
        attachment = MessageAttachment(
            message_id=message.id,
            raw_event_id=raw_event.id,
            channel_id=channel.id,
            conversation_id=conversation.id,
            direction=attachment_direction,
            attachment_type=attachment_type,
            provider="workwechat" if channel.channel_type == "workwechat" else "wechat_winauto",
        )
        db.add(attachment)
    attachment.direction = attachment_direction
    attachment.status = _resolve_wechat_winauto_attachment_status(details)
    attachment.storage_path = str(details.get("download_path") or details.get("file_path") or "").strip() or None
    attachment.file_size = _coerce_optional_int(details.get("file_size") or details.get("size"))
    attachment.mime_type = str(details.get("mime_type") or details.get("content_type") or "").strip() or None
    attachment.last_error = str(details.get("download_error") or "").strip() or None
    attachment.metadata_json = json.dumps(
        {
            **_parse_json_text(attachment.metadata_json),
            "source_event_key": raw_event.event_key,
            "file_name": str(details.get("file_name") or details.get("filename") or "").strip() or None,
            "details": details,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    _reuse_cached_workwechat_attachment_if_possible(db, attachment=attachment, details=details)
    _queue_workwechat_file_fetch_compensation_if_needed(db, attachment=attachment)


def _apply_wechat_winauto_attachment_ready_if_needed(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
    raw_event: RawWebhookEvent,
    normalized: dict[str, Any],
) -> None:
    if channel.channel_type not in {"wechat_winauto", "workwechat"}:
        return
    if str(normalized.get("event_type") or "").strip() != "message_attachment_ready":
        return
    details = normalized.get("details")
    if not isinstance(details, dict):
        return
    source_event_key = str(details.get("attachment_event_for") or "").strip()
    attachment_details = details.get("attachment_details")
    if not source_event_key or not isinstance(attachment_details, dict):
        return
    source_raw_event = db.scalar(
        select(RawWebhookEvent).where(
            RawWebhookEvent.channel_id == channel.id,
            RawWebhookEvent.event_key == source_event_key,
        )
    )
    if not source_raw_event:
        return
    attachment_type = str(details.get("attachment_message_type") or "").strip() or "file"
    attachment_direction = _resolve_wechat_winauto_attachment_direction(normalized, details=attachment_details)
    attachment = db.scalar(
        select(MessageAttachment).where(
            MessageAttachment.raw_event_id == source_raw_event.id,
            MessageAttachment.channel_id == channel.id,
            MessageAttachment.attachment_type == attachment_type,
            MessageAttachment.provider.in_(("wechat_winauto", "workwechat")),
        )
    )
    if not attachment:
        attachment = MessageAttachment(
            raw_event_id=source_raw_event.id,
            channel_id=channel.id,
            conversation_id=conversation.id,
            direction=attachment_direction,
            attachment_type=attachment_type,
            provider="workwechat" if channel.channel_type == "workwechat" else "wechat_winauto",
        )
        db.add(attachment)
    attachment.direction = attachment_direction
    attachment.status = _resolve_wechat_winauto_attachment_status(attachment_details)
    attachment.storage_path = str(attachment_details.get("download_path") or attachment_details.get("file_path") or "").strip() or None
    attachment.file_size = _coerce_optional_int(attachment_details.get("file_size") or attachment_details.get("size"))
    attachment.mime_type = str(attachment_details.get("mime_type") or attachment_details.get("content_type") or "").strip() or None
    attachment.last_error = str(attachment_details.get("download_error") or "").strip() or None
    attachment.metadata_json = json.dumps(
        {
            **_parse_json_text(attachment.metadata_json),
            "source_event_key": source_event_key,
            "attachment_ready_event_key": raw_event.event_key,
            "file_name": str(attachment_details.get("file_name") or attachment_details.get("filename") or "").strip() or None,
            "details": attachment_details,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    _reuse_cached_workwechat_attachment_if_possible(db, attachment=attachment, details=attachment_details)
    _queue_workwechat_file_fetch_compensation_if_needed(db, attachment=attachment)


def _attach_message_attachments_to_normalized_event(
    db: Session,
    *,
    channel: Channel,
    raw_event: RawWebhookEvent,
    normalized: dict[str, Any],
    message_id: int | None,
) -> None:
    if channel.channel_type not in {"wechat_winauto", "workwechat", "wechat_ipad", "wechat_webridge"}:
        return
    stmt = select(MessageAttachment).where(
        MessageAttachment.channel_id == channel.id,
        MessageAttachment.raw_event_id == raw_event.id,
    )
    if message_id:
        # 同一个原始事件只应关联当前消息附件；额外保留 raw_event_id 条件，避免误把其它补偿事件的附件混进来。
        stmt = stmt.where((MessageAttachment.message_id == message_id) | (MessageAttachment.message_id.is_(None)))
    rows = db.scalars(stmt.order_by(MessageAttachment.id.asc())).all()
    attachments = [_serialize_message_attachment(row) for row in rows]
    if not attachments:
        return
    # 插件任务异步执行，不能依赖 SQLAlchemy Session 仍然可用；这里把附件关键信息固化到事件快照中。
    normalized["attachments"] = attachments
    details = normalized.get("details")
    if isinstance(details, dict):
        details["attachments"] = attachments


def _load_ready_message_attachments(db: Session, *, channel: Channel, message: Message) -> list[MessageAttachment]:
    """读取已经可供业务系统处理的入站附件。"""

    return db.scalars(
        select(MessageAttachment).where(
            MessageAttachment.channel_id == channel.id,
            MessageAttachment.message_id == message.id,
            MessageAttachment.direction == "in",
            MessageAttachment.status == "downloaded",
        )
    ).all()


def _queue_workwechat_file_fetch_compensation_if_needed(db: Session, *, attachment: MessageAttachment) -> None:
    """为企微入站文件安排一次 CDN 补偿下载。

    企微 Hook 有时会先给出 Windows 本地缓存路径，但主服务在 Linux 上无法读取这个路径。
    如果事件里还保留了 CDN 元数据，就把附件交给后台 worker 重试下载，成功后复用现有
    file.received 开放回调链路，避免回单文件停在 failed 状态后无人处理。
    """

    if attachment.provider != "workwechat" or attachment.attachment_type != "file":
        return
    if attachment.direction != "in" or not attachment.message_id:
        return
    if attachment.status == "downloaded" or attachment.storage_path:
        return
    if not _workwechat_attachment_has_downloadable_cdn(attachment):
        return
    existing = db.scalar(
        select(TaskJob).where(
            TaskJob.job_type == INBOUND_MEDIA_FETCH_JOB,
            TaskJob.related_type == "message_attachment",
            TaskJob.related_id == attachment.id,
            TaskJob.status.in_(("pending", "running")),
        )
    )
    if existing:
        return
    channel = db.get(Channel, int(attachment.channel_id or 0))
    if channel is not None and is_cloud_poll_enabled(channel):
        _queue_workwechat_cloud_file_fetch_compensation(db, channel=channel, attachment=attachment)
        return
    # 保留 last_error 便于排查原始失败原因，但状态改为 pending，让后台任务接管补偿下载。
    attachment.status = "pending"
    enqueue_job(
        db,
        job_type=INBOUND_MEDIA_FETCH_JOB,
        payload={
            "attachment_id": attachment.id,
            "channel_id": attachment.channel_id,
            "provider": "workwechat",
            "reason": "workwechat_cdn_compensation",
        },
        priority=96,
        related_type="message_attachment",
        related_id=attachment.id,
        max_attempts=3,
    )


def _queue_workwechat_cloud_file_fetch_compensation(
    db: Session,
    *,
    channel: Channel,
    attachment: MessageAttachment,
) -> None:
    """云端企微客户端模式下，把补偿下载下发成本地 provider 控制任务。"""

    existing = db.scalar(
        select(TaskJob).where(
            TaskJob.job_type == WORKWECHAT_CLOUD_CONTROL_JOB,
            TaskJob.related_type == "message_attachment",
            TaskJob.related_id == attachment.id,
            TaskJob.status.in_(("pending", "running")),
        )
    )
    if existing:
        return
    metadata = _parse_json_text(attachment.metadata_json)
    details = metadata.get("details") if isinstance(metadata.get("details"), dict) else {}
    request_data = _build_workwechat_cdn_request_data_for_cloud(attachment=attachment, metadata=metadata, details=details)
    attachment.status = "pending"
    job = enqueue_job(
        db,
        job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
        payload={
            "channel_id": channel.id,
            "action": "download_inbound_media",
            "attachment_id": attachment.id,
            "payload": {
                "attachment_id": attachment.id,
                "message_type": attachment.attachment_type,
                "source_event_key": str(metadata.get("source_event_key") or "").strip() or None,
                "data": request_data,
            },
        },
        priority=96,
        related_type="message_attachment",
        related_id=attachment.id,
        max_attempts=3,
    )
    notify_cloud_job_enqueued(channel, job)


def _build_workwechat_cdn_request_data_for_cloud(
    *,
    attachment: MessageAttachment,
    metadata: dict[str, Any],
    details: dict[str, Any],
) -> dict[str, Any]:
    raw_data = details.get("raw_data") if isinstance(details.get("raw_data"), dict) else {}
    request_data = dict(raw_data)
    cdn = details.get("cdn") if isinstance(details.get("cdn"), dict) else {}
    if not cdn:
        cdn = request_data.get("cdn") if isinstance(request_data.get("cdn"), dict) else {}
    if not cdn:
        cdn = request_data.get("c2c_cdn") if isinstance(request_data.get("c2c_cdn"), dict) else {}
    if cdn:
        request_data.setdefault("cdn", dict(cdn))
    if details.get("cdn_type") not in (None, ""):
        request_data.setdefault("cdn_type", details.get("cdn_type"))
    if details.get("file_name") or metadata.get("file_name"):
        request_data.setdefault("file_name", details.get("file_name") or metadata.get("file_name"))
    if attachment.file_size or details.get("file_size"):
        request_data.setdefault("file_size", attachment.file_size or details.get("file_size"))
    source_event_key = str(metadata.get("source_event_key") or "").strip()
    if source_event_key:
        request_data.setdefault("server_id", source_event_key)
    return request_data


def _workwechat_attachment_has_downloadable_cdn(attachment: MessageAttachment) -> bool:
    metadata = _parse_json_text(attachment.metadata_json)
    details = metadata.get("details") if isinstance(metadata.get("details"), dict) else {}
    raw_data = details.get("raw_data") if isinstance(details.get("raw_data"), dict) else {}
    cdn = details.get("cdn") if isinstance(details.get("cdn"), dict) else {}
    if not cdn:
        cdn = raw_data.get("cdn") if isinstance(raw_data.get("cdn"), dict) else {}
    if not cdn:
        cdn = raw_data.get("c2c_cdn") if isinstance(raw_data.get("c2c_cdn"), dict) else {}
    has_wx_cdn = bool(cdn.get("url") and cdn.get("auth_key") and cdn.get("aes_key"))
    has_c2c_cdn = bool(cdn.get("file_id") and cdn.get("aes_key"))
    return has_wx_cdn or has_c2c_cdn


def _serialize_message_attachment(attachment: MessageAttachment) -> dict[str, Any]:
    metadata = _parse_json_text(attachment.metadata_json)
    details = metadata.get("details") if isinstance(metadata.get("details"), dict) else {}
    file_name = (
        str(metadata.get("file_name") or "").strip()
        or str(details.get("file_name") or details.get("filename") or "").strip()
        or None
    )
    file_size = attachment.file_size or _coerce_optional_int(details.get("file_size") or details.get("size"))
    return {
        "id": attachment.id,
        "message_id": attachment.message_id,
        "raw_event_id": attachment.raw_event_id,
        "channel_id": attachment.channel_id,
        "conversation_id": attachment.conversation_id,
        "direction": attachment.direction,
        "type": attachment.attachment_type,
        "attachment_type": attachment.attachment_type,
        "provider": attachment.provider,
        "status": attachment.status,
        "file_name": file_name,
        "storage_path": attachment.storage_path,
        "local_path": attachment.storage_path,
        "mime_type": attachment.mime_type,
        "file_size": file_size,
        "last_error": attachment.last_error,
        "metadata": metadata,
    }


def _resolve_wechat_winauto_attachment_status(details: dict[str, Any]) -> str:
    if str(details.get("download_path") or details.get("file_path") or "").strip():
        return "downloaded"
    if str(details.get("download_error") or "").strip():
        return "failed"
    if bool(details.get("download_pending")):
        return "pending"
    return "received"


def _reuse_cached_workwechat_attachment_if_possible(db: Session, *, attachment: MessageAttachment, details: dict[str, Any]) -> None:
    """Recover transient WorkWechat CDN failures by reusing an identical downloaded file."""

    if attachment.provider != "workwechat" or attachment.attachment_type != "file":
        return
    if attachment.storage_path or attachment.status == "downloaded":
        return
    fingerprint = _extract_attachment_fingerprint(details)
    if fingerprint is None:
        return
    md5_value, file_size = fingerprint
    candidates = db.scalars(
        select(MessageAttachment)
        .where(
            MessageAttachment.channel_id == attachment.channel_id,
            MessageAttachment.direction == attachment.direction,
            MessageAttachment.attachment_type == attachment.attachment_type,
            MessageAttachment.provider == attachment.provider,
            MessageAttachment.status == "downloaded",
            MessageAttachment.storage_path.is_not(None),
            MessageAttachment.file_size == file_size,
            MessageAttachment.id != attachment.id,
        )
        .order_by(MessageAttachment.id.desc())
    ).all()
    for candidate in candidates:
        candidate_fingerprint = _extract_attachment_fingerprint(_parse_json_text(candidate.metadata_json))
        if candidate_fingerprint != (md5_value, file_size):
            continue
        cached_path = str(candidate.storage_path or "").strip()
        if not cached_path:
            continue
        attachment.status = "downloaded"
        attachment.storage_path = cached_path
        attachment.file_size = file_size
        attachment.mime_type = attachment.mime_type or candidate.mime_type
        attachment.last_error = None
        metadata = _parse_json_text(attachment.metadata_json)
        metadata["cache_reused_from_attachment_id"] = candidate.id
        metadata["cache_reused_reason"] = "matched_cdn_md5_and_size"
        current_details = metadata.get("details") if isinstance(metadata.get("details"), dict) else dict(details)
        current_details["download_path"] = cached_path
        current_details["file_path"] = cached_path
        current_details["download_error"] = None
        metadata["details"] = current_details
        attachment.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        db.flush()
        logger.info(
            "reused cached workwechat attachment",
            extra={
                "attachment_id": attachment.id,
                "cached_attachment_id": candidate.id,
                "channel_id": attachment.channel_id,
                "file_size": file_size,
            },
        )
        return


def _extract_attachment_fingerprint(payload: dict[str, Any]) -> tuple[str, int] | None:
    details = payload.get("details") if isinstance(payload.get("details"), dict) else payload
    cdn = details.get("cdn") if isinstance(details.get("cdn"), dict) else {}
    md5_value = str(cdn.get("md5") or details.get("md5") or "").strip().lower()
    file_size = _coerce_optional_int(cdn.get("size") or details.get("file_size") or details.get("size"))
    if not md5_value or not file_size:
        return None
    return md5_value, file_size


def _parse_json_text(value: str | None) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_optional_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
