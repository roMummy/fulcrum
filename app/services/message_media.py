"""消息中心的媒体按需加载:点击占位卡片时,让手机把这条消息的图片/视频/语音/表情/文件传上来,页面轮询到就显示。

背景:微桥的媒体文件只在手机本地,默认不自动下载(手机上传一次要占用手机的串行通道,会拖慢消息上报),
所以大多数媒体消息没有附件记录或附件是 pending。这里复用入站自动下载用的那一套:
建附件记录 → 下发 upload_message_media 命令 → 手机把字节 POST 到 /agent/upload_media → 附件变成 downloaded。

同一条消息同一时间只下发一次(附件元数据里记着任务 ID 和时间);手机上传失败、或超过 REQUEST_TIMEOUT_SECONDS 还没传回,
都会把附件标成 failed,页面显示"获取失败,点击重试"。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Conversation, Message, MessageAttachment, RawWebhookEvent, TaskJob
from app.services.inbound_media import create_wechat_media_attachment, mark_attachment_failed
from app.services.task_queue import INBOUND_MEDIA_FETCH_JOB, enqueue_job
from app.services.wechat_webridge.service import build_upload_url, enqueue_media_upload_command

MEDIA_TYPES = {"image", "video", "voice", "emoji", "file"}
# 下发后这么久还没传回,就当作失败(手机离线、文件已过期等)。
REQUEST_TIMEOUT_SECONDS = 150


def _latest_attachment(db: Session, message: Message) -> MessageAttachment | None:
    return db.scalar(
        select(MessageAttachment)
        .where(MessageAttachment.message_id == message.id, MessageAttachment.attachment_type == message.message_type)
        .order_by(MessageAttachment.id.desc())
        .limit(1)
    )


def _meta(attachment: MessageAttachment) -> dict[str, Any]:
    try:
        value = json.loads(attachment.metadata_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _result(state: str, attachment: MessageAttachment | None = None, error: str = "") -> dict[str, Any]:
    ready = state == "ready" and attachment is not None
    return {"state": state, "attachment_id": int(attachment.id) if attachment is not None else None, "url": f"/media/attachments/{attachment.id}" if ready else "", "error": error}


def status(db: Session, message: Message) -> dict[str, Any]:
    """当前状态:ready 已可显示 / loading 手机正在传 / failed 失败 / none 还没请求过。

    顺便把"任务失败"或"超时"落实到附件上,页面轮询到 failed 就可以停下并提示重试。
    """

    if message.message_type not in MEDIA_TYPES:
        raise ValueError("这条消息不是媒体消息")
    attachment = _latest_attachment(db, message)
    if attachment is None:
        return _result("none")
    if attachment.status == "downloaded" and attachment.storage_path:
        return _result("ready", attachment)
    if attachment.status == "failed":
        return _result("failed", attachment, attachment.last_error or "获取失败")
    meta = _meta(attachment)
    job_id, requested_at = meta.get("upload_job_id"), meta.get("requested_at")
    if not job_id or not requested_at:
        return _result("none", attachment)  # 有记录但没人请求过(例如自动下载关闭时留下的)
    job = db.get(TaskJob, int(job_id))
    if job is not None and job.status == "failed":
        mark_attachment_failed(db, attachment=attachment, error_message=(job.error_message or "手机没有传回文件")[:200])
        return _result("failed", attachment, attachment.last_error)
    started = ensure_shanghai_datetime(_parse(requested_at))
    if now_shanghai() - started > timedelta(seconds=REQUEST_TIMEOUT_SECONDS):
        mark_attachment_failed(db, attachment=attachment, error_message="获取超时，请确认手机在线后重试")
        return _result("failed", attachment, attachment.last_error)
    return _result("loading", attachment)


def _parse(value: Any):
    from datetime import datetime

    return datetime.fromisoformat(str(value))


def request(db: Session, message: Message) -> dict[str, Any]:
    """请求把这条消息的媒体传上来。已有文件/已在获取中就直接返回当前状态;否则下发上传命令。调用方负责提交。"""

    current = status(db, message)
    if current["state"] in {"ready", "loading"}:
        return current
    conversation = db.get(Conversation, message.conversation_id)
    channel = db.get(Channel, conversation.channel_id) if conversation is not None and conversation.channel_id else None
    if channel is None:
        return _result("failed", None, "这个渠道暂不支持按需获取媒体")
    if channel.channel_type == "wechat_gugatool":
        # iOS 渠道的媒体在手机本地，按 DownloadMessageMedia → DownloadFile 两跳取回，
        # 复用入站媒体任务的下载实现，这里只负责建附件记录并入队。
        return _request_wechat_gugatool(db, message=message, channel=channel, conversation=conversation)
    if channel.channel_type != "wechat_webridge":
        return _result("failed", None, "这个渠道暂不支持按需获取媒体")
    if not message.external_message_id:
        return _result("failed", None, "缺少微信消息编号，无法获取")
    attachment = _latest_attachment(db, message)
    if attachment is None:
        attachment = create_wechat_media_attachment(
            db, channel=channel, raw_event_id=None, conversation_id=conversation.id, message_id=message.id,
            attachment_type=message.message_type, metadata={}, provider=channel.channel_type,
        )
    attachment.status = "pending"
    attachment.last_error = None
    job = enqueue_media_upload_command(
        db, channel=channel, conversation=conversation, message_id=str(message.external_message_id), media_type=message.message_type,
        upload_url=build_upload_url(channel, attachment.id), attachment_id=attachment.id,
    )
    attachment.metadata_json = json.dumps({**_meta(attachment), "upload_job_id": int(job.id), "requested_at": now_shanghai().isoformat()}, ensure_ascii=False, sort_keys=True)
    db.flush()
    return _result("loading", attachment)


def _request_wechat_gugatool(
    db: Session,
    *,
    message: Message,
    channel: Channel,
    conversation: Conversation,
) -> dict[str, Any]:
    """iOS 渠道的按需取媒体：建附件 + 入队入站媒体任务，由后台两跳取回。"""

    attachment = _latest_attachment(db, message)
    metadata = _wechat_gugatool_media_metadata(
        db,
        message=message,
        channel=channel,
        conversation=conversation,
        attachment=attachment,
    )
    if not metadata.get("local_message_id") and not metadata.get("server_message_id"):
        return _result("failed", attachment, "缺少微信消息编号，无法获取")
    if attachment is None:
        attachment = create_wechat_media_attachment(
            db,
            channel=channel,
            raw_event_id=None,
            conversation_id=conversation.id,
            message_id=message.id,
            attachment_type=message.message_type,
            metadata=metadata,
            provider=channel.channel_type,
        )
    attachment.status = "pending"
    attachment.last_error = None
    job = enqueue_job(
        db,
        job_type=INBOUND_MEDIA_FETCH_JOB,
        payload={"attachment_id": attachment.id, "channel_id": channel.id},
        priority=95,
        related_type="message_attachment",
        related_id=attachment.id,
    )
    # upload_job_id/requested_at 是页面轮询判断"是否在获取中/是否超时"的依据。
    attachment.metadata_json = json.dumps(
        {**metadata, "upload_job_id": int(job.id), "requested_at": now_shanghai().isoformat()},
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return _result("loading", attachment)


def _wechat_gugatool_media_metadata(
    db: Session,
    *,
    message: Message,
    channel: Channel,
    conversation: Conversation,
    attachment: MessageAttachment | None,
) -> dict[str, Any]:
    """优先用附件里已有的定位信息；没有就回查原始事件里的微信消息 ID。"""

    existing = _meta(attachment) if attachment is not None else {}
    if existing.get("local_message_id") or existing.get("server_message_id"):
        return {**existing, "to_wxid": existing.get("to_wxid") or conversation.external_id}

    payload: dict[str, Any] = {}
    if message.external_message_id:
        event = db.scalar(
            select(RawWebhookEvent).where(
                RawWebhookEvent.channel_id == channel.id,
                RawWebhookEvent.event_key == message.external_message_id,
            )
        )
        if event is not None:
            try:
                parsed = json.loads(event.payload_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                payload = parsed
    return {
        "to_wxid": conversation.external_id,
        "local_message_id": payload.get("localMessageID") or None,
        "server_message_id": payload.get("serverMessageID") or None,
        "message_type": payload.get("messageType") or None,
    }
