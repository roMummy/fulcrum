from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request as urlrequest

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.entities import Channel, MessageAttachment, TaskJob
from app.services.channel_adapters.workwechat import WorkWechatAdapter
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, enqueue_job
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.workwechat_cloud_poll import is_cloud_poll_enabled, notify_cloud_job_enqueued


def create_wechat_image_attachment(
    db: Session,
    *,
    channel: Channel,
    raw_event_id: int,
    conversation_id: int,
    message_id: int,
    image_info: dict[str, Any],
) -> MessageAttachment:
    attachment = MessageAttachment(
        message_id=message_id,
        raw_event_id=raw_event_id,
        channel_id=channel.id,
        conversation_id=conversation_id,
        direction="in",
        attachment_type="image",
        provider="wechat_ipad",
        status="pending",
        metadata_json=json.dumps(image_info, ensure_ascii=False, sort_keys=True),
    )
    db.add(attachment)
    db.flush()
    return attachment


def create_wechat_media_attachment(
    db: Session,
    *,
    channel: Channel,
    raw_event_id: int,
    conversation_id: int,
    message_id: int,
    attachment_type: str,
    metadata: dict[str, Any],
    provider: str = "wechat_ipad",
) -> MessageAttachment:
    attachment = MessageAttachment(
        message_id=message_id,
        raw_event_id=raw_event_id,
        channel_id=channel.id,
        conversation_id=conversation_id,
        direction="in",
        attachment_type=attachment_type,
        provider=provider,
        status="pending",
        metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
    )
    db.add(attachment)
    db.flush()
    return attachment


def download_wechat_image_attachment(
    db: Session,
    *,
    attachment: MessageAttachment,
    channel: Channel,
) -> dict[str, Any]:
    metadata = _parse_json(attachment.metadata_json)
    adapter_config = _parse_json(channel.adapter_config_json)
    wxid = str(adapter_config.get("wxid") or "").strip()
    file_aes_key = str(metadata.get("aeskey") or metadata.get("file_aes_key") or "").strip()
    file_no = str(metadata.get("file_no") or metadata.get("url") or "").strip()
    if not wxid:
        raise ValueError("wechat_ipad_wxid_missing")
    if not file_aes_key or not file_no:
        raise ValueError("wechat_ipad_image_metadata_missing")

    client = WechatIpadApiClient()
    response = client.download_image(
        channel,
        wxid=wxid,
        file_aes_key=file_aes_key,
        file_no=file_no,
    )
    resolved = _resolve_image_response(response)
    content = resolved["content"]
    if not content:
        raise ValueError("wechat_ipad_image_content_empty")

    storage_path = _build_attachment_path(channel.id, attachment.id, resolved["extension"])
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(content)

    attachment.status = "downloaded"
    attachment.storage_path = str(storage_path)
    attachment.mime_type = resolved["mime_type"]
    attachment.file_size = len(content)
    attachment.last_error = None
    attachment.metadata_json = json.dumps(
        {
            **metadata,
            "saved_at": datetime.now().isoformat(),
            "source_kind": resolved["source_kind"],
            "http_status": response.get("http_status"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return {
        "attachment_id": attachment.id,
        "storage_path": str(storage_path),
        "file_size": len(content),
        "mime_type": resolved["mime_type"],
    }


def download_wechat_media_attachment(
    db: Session,
    *,
    attachment: MessageAttachment,
    channel: Channel,
) -> dict[str, Any]:
    if attachment.provider == "wechat_gugatool":
        return _download_wechat_gugatool_attachment(db, attachment=attachment, channel=channel)
    if attachment.provider == "workwechat" and attachment.attachment_type == "file":
        return _download_workwechat_file_attachment(db, attachment=attachment, channel=channel)
    if attachment.attachment_type == "image":
        return download_wechat_image_attachment(db, attachment=attachment, channel=channel)
    if attachment.attachment_type == "voice":
        return _download_wechat_voice_attachment(db, attachment=attachment, channel=channel)
    if attachment.attachment_type == "video":
        return _download_wechat_video_attachment(db, attachment=attachment, channel=channel)
    raise ValueError(f"unsupported_attachment_type:{attachment.attachment_type}")


def mark_attachment_failed(db: Session, *, attachment: MessageAttachment, error_message: str) -> None:
    attachment.status = "failed"
    attachment.last_error = error_message
    db.flush()


def store_uploaded_media_attachment(
    db: Session,
    *,
    attachment: MessageAttachment,
    content: bytes,
    filename: str = "",
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """保存手机回传的媒体字节，并把附件切换为可消费状态。"""

    if not content:
        raise ValueError("webridge_uploaded_media_empty")
    suffix = Path(filename or "").suffix or _extension_for_mime(content_type)
    storage_path = _build_attachment_path(attachment.channel_id, attachment.id, suffix)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(content)
    attachment.status = "downloaded"
    attachment.storage_path = str(storage_path)
    attachment.file_size = len(content)
    attachment.mime_type = content_type or "application/octet-stream"
    attachment.last_error = None
    metadata = _parse_json(attachment.metadata_json)
    attachment.metadata_json = json.dumps({**metadata, "uploaded_by": "wechat_webridge", "file_name": filename or None}, ensure_ascii=False, sort_keys=True)
    db.flush()
    return {"attachment_id": int(attachment.id), "storage_path": str(storage_path), "file_size": len(content), "mime_type": attachment.mime_type}


def _extension_for_mime(content_type: str) -> str:
    """为没有文件名的上传提供保守扩展名，避免把媒体保存成无后缀临时文件。"""

    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "audio/mpeg": ".mp3",
        "audio/amr": ".amr",
        "video/mp4": ".mp4",
    }.get(str(content_type or "").lower(), ".bin")


def _download_workwechat_file_attachment(db: Session, *, attachment: MessageAttachment, channel: Channel) -> dict[str, Any]:
    """调用企微 provider 对失败文件做 CDN 补偿下载。

    主服务不能直接读取客服电脑上的 C:\\Users\\... 缓存路径；provider 才拥有 Hook Runtime
    和 CDN 下载能力。因此这里只把原始 CDN 元数据回传给 provider，由 provider 下载后再上传
    回主服务，最后把主服务自己的 storage_path 写回附件记录。
    """

    metadata = _parse_json(attachment.metadata_json)
    details = metadata.get("details") if isinstance(metadata.get("details"), dict) else {}
    request_data = _build_workwechat_cdn_request_data(attachment=attachment, metadata=metadata, details=details)
    if not _has_workwechat_cdn_payload(request_data):
        raise ValueError("workwechat_cdn_metadata_missing")
    if is_cloud_poll_enabled(channel):
        return _queue_workwechat_cloud_file_attachment_download(
            db,
            channel=channel,
            attachment=attachment,
            metadata=metadata,
            request_data=request_data,
        )
    if not channel.api_base:
        raise ValueError("workwechat_provider_api_base_missing")

    adapter = WorkWechatAdapter()
    adapter_config = adapter._load_adapter_config(channel)
    target_url = adapter._build_absolute_url(channel.api_base, "/inbound-media/download")
    headers = adapter._build_headers(channel, {}, adapter_config)
    response = adapter._post_json(
        target_url,
        {
            "attachment_id": attachment.id,
            "message_type": attachment.attachment_type,
            "source_event_key": str(metadata.get("source_event_key") or "").strip() or None,
            "data": request_data,
        },
        headers=headers,
        timeout=max(30, _coerce_optional_int(adapter_config.get("inbound_file_download_timeout_seconds")) or 60),
    )
    if not adapter._response_indicates_success(response) or response.get("download_error"):
        raise ValueError(str(response.get("download_error") or response.get("error") or response.get("message") or "workwechat_cdn_download_failed"))

    storage_path = str(
        response.get("download_path")
        or response.get("storage_path")
        or response.get("file_path")
        or ""
    ).strip()
    if not storage_path:
        raise ValueError("workwechat_cdn_download_missing_storage_path")

    file_size = _coerce_optional_int(response.get("file_size")) or attachment.file_size
    attachment.status = "downloaded"
    attachment.storage_path = storage_path
    attachment.file_size = file_size
    attachment.mime_type = str(response.get("mime_type") or response.get("content_type") or "application/octet-stream").strip()
    attachment.last_error = None
    attachment.metadata_json = json.dumps(
        {
            **metadata,
            "workwechat_cdn_compensation": {
                "downloaded_at": datetime.now().isoformat(),
                "provider_url": target_url,
                "source_event_key": metadata.get("source_event_key"),
                "download_url": response.get("download_url"),
                "source_path": response.get("source_path"),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return {
        "attachment_id": attachment.id,
        "storage_path": storage_path,
        "file_size": file_size,
        "mime_type": attachment.mime_type,
        "provider": "workwechat",
        "compensation": True,
    }


def _queue_workwechat_cloud_file_attachment_download(
    db: Session,
    *,
    channel: Channel,
    attachment: MessageAttachment,
    metadata: dict[str, Any],
    request_data: dict[str, Any],
) -> dict[str, Any]:
    """把历史直连补偿任务转换为云端控制任务。

    cloud_poll 模式下服务端无法访问客服电脑的 8181/8989，本地 provider 才能调用 Hook/CDN。
    因此即使旧的 inbound_media_fetch 任务被 worker 捞到，也不能再尝试直连 api_base。
    """

    existing = db.scalar(
        select(TaskJob).where(
            TaskJob.job_type == WORKWECHAT_CLOUD_CONTROL_JOB,
            TaskJob.related_type == "message_attachment",
            TaskJob.related_id == attachment.id,
            TaskJob.status.in_(("pending", "running")),
        )
    )
    attachment.status = "pending"
    if existing:
        db.flush()
        return {
            "attachment_id": attachment.id,
            "provider": "workwechat",
            "queued_cloud_compensation": True,
            "cloud_job_id": existing.id,
            "deduplicated": True,
        }
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
    return {
        "attachment_id": attachment.id,
        "provider": "workwechat",
        "queued_cloud_compensation": True,
        "cloud_job_id": job.id,
        "deduplicated": False,
    }


def _build_workwechat_cdn_request_data(
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
    # 部分现场事件把 cdn_type 放在 raw_data 顶层；这里保留并补齐文件名/大小，保证 provider 能选对下载命令。
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


def _has_workwechat_cdn_payload(data: dict[str, Any]) -> bool:
    cdn = data.get("cdn") if isinstance(data.get("cdn"), dict) else {}
    if not cdn:
        cdn = data.get("c2c_cdn") if isinstance(data.get("c2c_cdn"), dict) else {}
    return bool((cdn.get("url") and cdn.get("auth_key") and cdn.get("aes_key")) or (cdn.get("file_id") and cdn.get("aes_key")))


def _resolve_image_response(response: dict[str, Any]) -> dict[str, Any]:
    base64_payload = _pick_value(
        response,
        ("Data", "ImageBase64"),
        ("Data", "Base64"),
        ("ImageBase64",),
        ("Base64",),
        ("base64",),
    )
    normalized = WechatIpadApiClient.normalize_qr_image_source(base64_payload)
    if normalized["base64"]:
        content = base64.b64decode(str(normalized["base64"]), validate=True)
        mime_type = str(normalized["media_type"] or "image/jpeg")
        return {
            "content": content,
            "mime_type": mime_type,
            "extension": _guess_extension(mime_type, content),
            "source_kind": "base64",
        }

    image_url = _pick_value(
        response,
        ("Data", "ImageUrl"),
        ("Data", "Url"),
        ("ImageUrl",),
        ("Url",),
        ("url",),
    )
    if image_url:
        req = urlrequest.Request(str(image_url), method="GET")
        with urlrequest.urlopen(req, timeout=20) as remote:
            content = remote.read()
            mime_type = str(getattr(remote.headers, "get_content_type", lambda: "image/jpeg")() or "image/jpeg")
        return {
            "content": content,
            "mime_type": mime_type,
            "extension": _guess_extension(mime_type, content),
            "source_kind": "url",
        }

    raise ValueError("wechat_ipad_image_response_missing_payload")


def _download_wechat_voice_attachment(db: Session, *, attachment: MessageAttachment, channel: Channel) -> dict[str, Any]:
    metadata = _parse_json(attachment.metadata_json)
    adapter_config = _parse_json(channel.adapter_config_json)
    wxid = str(adapter_config.get("wxid") or "").strip()
    msg_id = str(metadata.get("msg_id") or "").strip()
    from_user_name = str(metadata.get("from_user_name") or "").strip()
    bufid = str(metadata.get("bufid") or "").strip()
    length = int(str(metadata.get("length") or "0").strip() or 0)
    if not wxid or not msg_id or not from_user_name or not bufid or length <= 0:
        raise ValueError("wechat_ipad_voice_metadata_missing")
    client = WechatIpadApiClient()
    response = client.download_voice(
        channel,
        wxid=wxid,
        msg_id=msg_id,
        from_user_name=from_user_name,
        bufid=bufid,
        length=length,
    )
    resolved = _resolve_binary_response(response, default_mime_type="audio/amr", default_extension=".amr")
    return _persist_attachment_content(db, attachment=attachment, response=response, resolved=resolved, metadata=metadata)


def _download_wechat_video_attachment(db: Session, *, attachment: MessageAttachment, channel: Channel) -> dict[str, Any]:
    metadata = _parse_json(attachment.metadata_json)
    adapter_config = _parse_json(channel.adapter_config_json)
    wxid = str(adapter_config.get("wxid") or "").strip()
    msg_id = str(metadata.get("msg_id") or "").strip()
    to_wxid = str(metadata.get("to_wxid") or "").strip()
    data_len = int(str(metadata.get("data_len") or "0").strip() or 0)
    start_pos = int(str(metadata.get("start_pos") or "0").strip() or 0)
    section_length = int(str(metadata.get("section_length") or data_len).strip() or 0)
    compress_type = int(str(metadata.get("compress_type") or "0").strip() or 0)
    if not wxid or not msg_id or not to_wxid or data_len <= 0 or section_length <= 0:
        raise ValueError("wechat_ipad_video_metadata_missing")
    client = WechatIpadApiClient()
    response = client.download_video(
        channel,
        wxid=wxid,
        msg_id=msg_id,
        to_wxid=to_wxid,
        data_len=data_len,
        start_pos=start_pos,
        section_length=section_length,
        compress_type=compress_type,
    )
    resolved = _resolve_binary_response(response, default_mime_type="video/mp4", default_extension=".mp4")
    return _persist_attachment_content(db, attachment=attachment, response=response, resolved=resolved, metadata=metadata)


def _download_wechat_gugatool_attachment(db: Session, *, attachment: MessageAttachment, channel: Channel) -> dict[str, Any]:
    """手机端先把消息媒体落到本地缓存，回传下载地址后主服务再取字节。

    走 ``DownloadMessageMedia`` + ``GET DownloadFile`` 两跳，避免要求主服务和
    手机在同一容器网络，也避免把 aeskey 之类的解密参数落到主服务。
    """

    from app.services.channel_adapters.wechat_gugatool import WechatGugatoolAdapter

    metadata = _parse_json(attachment.metadata_json)
    adapter = WechatGugatoolAdapter()
    response = adapter.download_message_media(
        channel,
        to_wxid=str(metadata.get("to_wxid") or ""),
        local_id=_coerce_optional_int(metadata.get("local_message_id")),
        msg_svr_id=_coerce_optional_int(metadata.get("server_message_id")),
    )
    data = response.get("data") or {}
    base64_payload = str(data.get("Base64") or data.get("base64") or "").strip()
    if not base64_payload:
        raise ValueError("wechat_gugatool_media_base64_missing")
    content = base64.b64decode(base64_payload, validate=False)
    mime_type = str(data.get("ContentType") or data.get("content_type") or "").strip()
    resolved_mime = mime_type or "application/octet-stream"
    source_kind = "base64"
    return _persist_attachment_content(
        db,
        attachment=attachment,
        response=response.get("raw") if isinstance(response.get("raw"), dict) else {},
        resolved={
            "content": content,
            "mime_type": resolved_mime,
            "extension": _guess_extension(resolved_mime, content),
            "source_kind": source_kind,
        },
        metadata=metadata,
    )


def _persist_attachment_content(
    db: Session,
    *,
    attachment: MessageAttachment,
    response: dict[str, Any],
    resolved: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    content = resolved["content"]
    if not content:
        raise ValueError("wechat_ipad_attachment_content_empty")
    storage_path = _build_attachment_path(attachment.channel_id, attachment.id, resolved["extension"])
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(content)
    attachment.status = "downloaded"
    attachment.storage_path = str(storage_path)
    attachment.mime_type = resolved["mime_type"]
    attachment.file_size = len(content)
    attachment.last_error = None
    attachment.metadata_json = json.dumps(
        {
            **metadata,
            "saved_at": datetime.now().isoformat(),
            "source_kind": resolved["source_kind"],
            "http_status": response.get("http_status"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.flush()
    return {
        "attachment_id": attachment.id,
        "storage_path": str(storage_path),
        "file_size": len(content),
        "mime_type": resolved["mime_type"],
    }


def _resolve_binary_response(response: dict[str, Any], *, default_mime_type: str, default_extension: str) -> dict[str, Any]:
    base64_payload = _pick_value(
        response,
        ("Data", "Base64"),
        ("Data", "base64"),
        ("Base64",),
        ("base64",),
    )
    if base64_payload:
        normalized = str(WechatIpadApiClient.unwrap_value(base64_payload) or "").strip()
        if normalized.startswith("data:") and "," in normalized:
            header, normalized = normalized.split(",", 1)
            mime_type = header.removeprefix("data:").split(";", 1)[0].strip() or default_mime_type
        else:
            mime_type = default_mime_type
        return {
            "content": base64.b64decode(normalized, validate=True),
            "mime_type": mime_type,
            "extension": default_extension,
            "source_kind": "base64",
        }
    media_url = _pick_value(
        response,
        ("Data", "Url"),
        ("Data", "url"),
        ("Url",),
        ("url",),
    )
    if media_url:
        req = urlrequest.Request(str(media_url), method="GET")
        with urlrequest.urlopen(req, timeout=20) as remote:
            content = remote.read()
            mime_type = str(getattr(remote.headers, "get_content_type", lambda: default_mime_type)() or default_mime_type)
        return {
            "content": content,
            "mime_type": mime_type,
            "extension": default_extension,
            "source_kind": "url",
        }
    raise ValueError("wechat_ipad_binary_response_missing_payload")


def _build_attachment_path(channel_id: int, attachment_id: int, extension: str) -> Path:
    now = datetime.now()
    return (
        Path(settings.data_dir)
        / "inbound_media"
        / f"channel_{channel_id}"
        / now.strftime("%Y%m%d")
        / f"attachment_{attachment_id}{extension}"
    )


def _guess_extension(mime_type: str, content: bytes) -> str:
    normalized = str(mime_type or "").strip().lower()
    if normalized == "image/png":
        return ".png"
    if normalized == "image/webp":
        return ".webp"
    if normalized == "image/gif":
        return ".gif"
    if normalized == "image/bmp":
        return ".bmp"
    if content.startswith(b"\x89PNG"):
        return ".png"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return ".gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _parse_json(raw: str | None) -> dict[str, Any]:
    normalized = str(raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
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


def _pick_value(payload: dict[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        current: Any = payload
        for part in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return current
    return None
