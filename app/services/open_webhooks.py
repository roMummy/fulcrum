from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from zipfile import BadZipFile, ZipFile

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, Message, MessageAttachment, OpenApiApp, OpenApiAppChannel, TaskJob
from app.services.task_queue import OPEN_WEBHOOK_DELIVERY_JOB, enqueue_job, parse_job_payload
from app.services.open_event_catalog import build_open_event_id, is_open_event_type


OPEN_WEBHOOK_TIMEOUT_SECONDS = 10
SUPPLIER_ORDER_APP_KEYS = {"supplier-order", "supplier_order", "supplier_order_flow"}
SUPPLIER_ORDER_SPLIT_LABELS = ("分单ID", "子单ID")
SUPPLIER_ORDER_TRACKING_LABELS = ("快递单号", "运单号")
SUPPLIER_ORDER_EXCEL_SUFFIXES = (".xls", ".xlsx")
logger = get_logger(__name__)


@dataclass(frozen=True)
class OpenWebhookDeliveryResult:
    """开放事件回调投递结果。"""

    app_key: str
    event_id: str
    ok: bool
    status_code: int
    error_message: str = ""


def sign_open_webhook_payload(secret: str, raw_body: bytes, timestamp: str) -> str:
    """生成开放事件 Webhook 签名。"""

    message = str(timestamp or "").encode("utf-8") + b"\n" + raw_body
    return hmac.new(str(secret or "").encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_message_event_envelope(
    *,
    event_type: str,
    channel: Channel,
    conversation: Conversation,
    message: Message,
    attachments: list[MessageAttachment] | None = None,
) -> dict[str, Any]:
    """构建对外事件信封。

    外部业务系统只接收统一结构，不直接暴露企业微信 Hook 或 Fulcrum 内部表字段；
    这样收发单系统、客服系统等业务都可以复用同一套事件协议。
    """

    normalized_event_type = str(event_type or "").strip()
    if not is_open_event_type(normalized_event_type):
        raise ValueError("open_event_type_not_supported")
    event_id = build_open_event_id(event_type=normalized_event_type, scope_id=channel.id, sequence=message.id)
    return {
        "event_id": event_id,
        "event_type": normalized_event_type,
        "occurred_at": now_shanghai().isoformat(),
        "bot": {"id": channel.bot_id},
        "account": {"id": channel.id, "channel_id": channel.id, "channel_type": channel.channel_type},
        "channel": {"id": channel.id, "type": channel.channel_type},
        "conversation": {
            "id": conversation.id,
            "external_id": conversation.external_id,
            "title": conversation.title,
            "chat_type": conversation.chat_type,
        },
        "sender": {
            "external_id": message.sender_id,
            "name": message.sender_name,
        },
        "message": {
            "id": message.id,
            "type": message.message_type,
            "content": message.content,
            "external_message_id": message.external_message_id,
            "status": message.status,
        },
        "attachments": [_serialize_attachment(item) for item in attachments or []],
    }


def list_webhook_apps_for_channel(db: Session, channel: Channel) -> list[OpenApiApp]:
    """查找需要接收指定渠道事件的开放应用。"""

    return db.scalars(
        select(OpenApiApp)
        .join(OpenApiAppChannel, OpenApiAppChannel.app_id == OpenApiApp.id)
        .where(
            OpenApiApp.status == "active",
            OpenApiApp.webhook_url.is_not(None),
            OpenApiAppChannel.channel_id == channel.id,
        )
    ).all()


def dispatch_open_message_event(
    db: Session,
    *,
    event_type: str,
    channel: Channel,
    conversation: Conversation,
    message: Message,
    attachments: list[MessageAttachment] | None = None,
) -> list[OpenWebhookDeliveryResult]:
    """把消息事件异步投递给绑定了当前渠道的开放应用。

    入站消息路径不能同步等待业务系统 webhook，否则客户业务系统慢、异常或误处理时，
    Fulcrum 的入站请求和消息落库提交都会被拖住。这里仅完成收发单过滤并创建投递任务，
    真正 HTTP 调用交给后台 worker。
    """

    apps = list_webhook_apps_for_channel(db, channel)
    if not apps:
        return []
    envelope = build_message_event_envelope(
        event_type=event_type,
        channel=channel,
        conversation=conversation,
        message=message,
        attachments=attachments or [],
    )
    results: list[OpenWebhookDeliveryResult] = []
    for app in apps:
        if not _should_deliver_message_event_to_app(app, event_type=event_type, message=message, attachments=attachments or []):
            logger.info(
                "open webhook skipped by app event filter",
                extra={
                    "app_key": app.app_key,
                    "event_type": event_type,
                    "message_id": message.id,
                    "channel_id": channel.id,
                    "reason": "supplier_order_payload_not_matched",
                },
            )
            continue
        job = enqueue_job(
            db,
            job_type=OPEN_WEBHOOK_DELIVERY_JOB,
            payload={
                "app_id": app.id,
                "app_key": app.app_key,
                "event_type": event_type,
                "event_id": envelope.get("event_id"),
                "envelope": envelope,
            },
            priority=80,
            related_type="open_api_app",
            related_id=app.id,
            max_attempts=3,
        )
        result = OpenWebhookDeliveryResult(
            app_key=app.app_key,
            event_id=str(envelope.get("event_id") or ""),
            ok=True,
            status_code=202,
            error_message=f"queued:{job.id}",
        )
        results.append(result)
        logger.info(
            "open webhook delivery job queued",
            extra={"app_key": result.app_key, "event_id": result.event_id, "event_type": event_type, "job_id": job.id},
        )
    return results


def process_open_webhook_delivery_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """执行开放 webhook 投递任务。

    worker 中失败会抛出异常交给任务队列重试；入站请求本身已经完成，不再被外部系统耗时拖慢。
    """

    app_id = int(payload.get("app_id") or 0)
    envelope = payload.get("envelope") if isinstance(payload.get("envelope"), dict) else {}
    app = db.get(OpenApiApp, app_id)
    if app is None:
        return {"ok": True, "skipped": True, "reason": "app_not_found", "app_id": app_id}
    if app.status != "active" or not str(app.webhook_url or "").strip():
        return {"ok": True, "skipped": True, "reason": "app_inactive_or_webhook_empty", "app_key": app.app_key}
    result = deliver_open_webhook(app, envelope)
    payload = {
        "ok": result.ok,
        "app_key": result.app_key,
        "event_id": result.event_id,
        "status_code": result.status_code,
        "error_message": result.error_message,
    }
    if result.ok:
        logger.info(
            "open webhook delivered",
            extra={
                "app_key": result.app_key,
                "event_id": result.event_id,
                "event_type": envelope.get("event_type"),
                "status_code": result.status_code,
            },
        )
        return payload
    logger.warning(
        "open webhook delivery failed",
        extra={
            "app_key": result.app_key,
            "event_id": result.event_id,
            "event_type": envelope.get("event_type"),
            "status_code": result.status_code,
            "error_message": result.error_message,
        },
    )
    raise RuntimeError(result.error_message or f"open webhook delivery failed: {result.status_code}")


def dispatch_open_send_result_event(
    db: Session,
    *,
    event_type: str,
    channel: Channel,
    conversation: Conversation,
    job: TaskJob,
    message: Message | None = None,
    error_message: str = "",
) -> list[OpenWebhookDeliveryResult]:
    """把开放接口发送结果事件放入后台投递队列。

    只有由 Open API 创建的任务才带 `open_api` 元数据；普通后台或插件发送任务会直接跳过。
    request_id 会原样回传给业务系统，用来更新新收发单系统里的批次发送状态。
    发送完成回调不能同步等待业务系统 webhook，否则供应商消息已经发出时，业务系统 401/超时
    会反向拖失败 provider 的 complete 请求，造成 Fulcrum 任务状态和真实发送状态不一致。
    """

    payload = parse_job_payload(job)
    open_api = payload.get("open_api") if isinstance(payload.get("open_api"), dict) else {}
    app_key = str(open_api.get("app_key") or "").strip()
    request_id = str(open_api.get("request_id") or "").strip()
    if not app_key or not request_id:
        return []
    app = db.scalar(select(OpenApiApp).where(OpenApiApp.app_key == app_key, OpenApiApp.status == "active"))
    if app is None or not str(app.webhook_url or "").strip():
        return []
    delivery = _build_send_result_delivery_snapshot(
        event_type=event_type,
        job=job,
        message=message,
        fallback_error_message=error_message,
    )
    envelope = {
        "event_id": build_open_event_id(event_type=event_type, scope_id=channel.id, sequence=job.id),
        "event_type": event_type,
        "occurred_at": now_shanghai().isoformat(),
        "open_api": {
            "app_key": app_key,
            "request_id": request_id,
            "job_id": job.id,
        },
        "bot": {"id": channel.bot_id},
        "account": {"id": channel.id, "channel_id": channel.id, "channel_type": channel.channel_type},
        "channel": {"id": channel.id, "type": channel.channel_type},
        "conversation": {
            "id": conversation.id,
            "external_id": conversation.external_id,
            "title": conversation.title,
            "chat_type": conversation.chat_type,
        },
        "message": {
            "id": message.id if message else None,
            "type": message.message_type if message else None,
            "content": message.content if message else "",
            "external_message_id": message.external_message_id if message else None,
            "status": message.status if message else "failed",
        },
        "delivery": delivery,
        "job": {
            "id": job.id,
            "status": job.status,
            "error_message": delivery["error_message"] or error_message or job.error_message,
            "error_summary": delivery["error_summary"],
            "delivery_status": delivery["status"],
            "delivery_confirmed": delivery["confirmed"],
        },
    }
    delivery_job = enqueue_job(
        db,
        job_type=OPEN_WEBHOOK_DELIVERY_JOB,
        payload={
            "app_id": app.id,
            "app_key": app.app_key,
            "event_type": event_type,
            "event_id": envelope.get("event_id"),
            "envelope": envelope,
        },
        priority=85,
        related_type="open_api_app",
        related_id=app.id,
        max_attempts=3,
    )
    result = OpenWebhookDeliveryResult(
        app_key=app.app_key,
        event_id=str(envelope.get("event_id") or ""),
        ok=True,
        status_code=202,
        error_message=f"queued:{delivery_job.id}",
    )
    logger.info(
        "open send result webhook delivery job queued",
        extra={
            "app_key": result.app_key,
            "request_id": request_id,
            "event_type": event_type,
            "job_id": job.id,
            "delivery_job_id": delivery_job.id,
        },
    )
    return [result]


def _build_send_result_delivery_snapshot(
    *,
    event_type: str,
    job: TaskJob,
    message: Message | None,
    fallback_error_message: str = "",
) -> dict[str, Any]:
    """生成开放发送结果里的投递状态快照。

    业务批次只应把 `message.failed` 当作明确失败；`message.sent` 里如果出现
    `accepted_unconfirmed`，表示 provider/Hook 已接收但 Fulcrum 没拿到自发回显，需要进入
    批次错误摘要提醒同事关注，不能触发自动补推。
    """

    adapter_result = _parse_json(message.raw_response_json) if message is not None else {}
    if not adapter_result:
        job_result = _parse_json(job.result_json)
        adapter_result = job_result.get("adapter_result") if isinstance(job_result.get("adapter_result"), dict) else {}

    external_message_id = str(
        (message.external_message_id if message is not None else None)
        or adapter_result.get("external_message_id")
        or ""
    ).strip()
    delivery_status = str(
        adapter_result.get("external_status")
        or adapter_result.get("status")
        or (message.external_status if message is not None else "")
        or ("failed" if event_type == "message.failed" else job.status)
        or ""
    ).strip()
    error_code = str(adapter_result.get("error_code") or "").strip()
    error_message = str(
        fallback_error_message
        or adapter_result.get("error_message")
        or job.error_message
        or ""
    ).strip()
    error_summary = str(adapter_result.get("error_summary") or "").strip() or _build_send_result_error_summary(
        event_type=event_type,
        delivery_status=delivery_status,
        error_code=error_code,
        error_message=error_message,
    )
    confirmed = (
        event_type == "message.sent"
        and bool(external_message_id)
        and delivery_status not in {"accepted_unconfirmed", "delivery_confirmation_timeout", "cloud_provider_lease_expired"}
    )
    return {
        "status": delivery_status,
        "confirmed": confirmed,
        "external_message_id": external_message_id or None,
        "error_code": error_code,
        "error_message": error_message,
        "error_summary": error_summary,
        "requires_attention": bool(error_summary),
    }


def _build_send_result_error_summary(
    *,
    event_type: str,
    delivery_status: str,
    error_code: str,
    error_message: str,
) -> str:
    """把发送结果压缩成业务批次可展示的一句话摘要。"""

    normalized_status = str(delivery_status or "").strip()
    normalized_code = str(error_code or "").strip()
    if normalized_status == "accepted_unconfirmed" or normalized_code in {"delivery_confirmation_timeout", "cloud_provider_lease_expired"}:
        return "企微投递未确认：系统未自动重发，请人工关注群内最新消息。"
    if event_type == "message.failed":
        return f"企微发送失败：{error_message or normalized_code or normalized_status or '未知错误'}，系统未自动重发，请人工处理。"
    return ""


def deliver_open_webhook(app: OpenApiApp, envelope: dict[str, Any]) -> OpenWebhookDeliveryResult:
    """向开放应用投递事件。

    首版同步投递，后续如果业务回调较多，可以把这里改为入队任务；
    当前先保证协议、签名和错误记录清晰可测。
    """

    webhook_url = str(app.webhook_url or "").strip()
    event_id = str(envelope.get("event_id") or "").strip()
    if not webhook_url:
        return OpenWebhookDeliveryResult(app_key=app.app_key, event_id=event_id, ok=False, status_code=0, error_message="webhook_url_empty")

    raw_body = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    req = urlrequest.Request(webhook_url, data=raw_body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-MBA-Event-ID", event_id)
    req.add_header("X-MBA-Timestamp", timestamp)
    if app.webhook_secret:
        req.add_header("X-MBA-Signature", sign_open_webhook_payload(app.webhook_secret, raw_body, timestamp))
    try:
        with urlrequest.urlopen(req, timeout=OPEN_WEBHOOK_TIMEOUT_SECONDS) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            return OpenWebhookDeliveryResult(app_key=app.app_key, event_id=event_id, ok=200 <= status_code < 300, status_code=status_code)
    except HTTPError as exc:
        return OpenWebhookDeliveryResult(app_key=app.app_key, event_id=event_id, ok=False, status_code=exc.code, error_message=str(exc))
    except URLError as exc:
        return OpenWebhookDeliveryResult(app_key=app.app_key, event_id=event_id, ok=False, status_code=0, error_message=str(exc.reason or exc))


def _serialize_attachment(attachment: MessageAttachment) -> dict[str, Any]:
    metadata = _parse_json(attachment.metadata_json)
    details = metadata.get("details") if isinstance(metadata.get("details"), dict) else {}
    file_name = (
        str(metadata.get("file_name") or "").strip()
        or str(details.get("file_name") or details.get("filename") or "").strip()
        or None
    )
    download_url = str(metadata.get("download_url") or details.get("download_url") or "").strip() or None
    return {
        "id": attachment.id,
        "type": attachment.attachment_type,
        "status": attachment.status,
        "file_name": file_name,
        "storage_path": attachment.storage_path,
        "download_url": download_url,
        "mime_type": attachment.mime_type,
        "file_size": attachment.file_size,
    }


def _parse_json(value: str | None) -> dict[str, Any]:
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _should_deliver_message_event_to_app(
    app: OpenApiApp,
    *,
    event_type: str,
    message: Message,
    attachments: list[MessageAttachment],
) -> bool:
    """判断开放消息事件是否应该投递给指定应用。

    收发单系统只需要接收真正像回单的数据；其他客服、监控或业务应用仍接收完整消息流。
    这里按 app_key 做定向过滤，避免为了收发单需求影响平台级开放接口能力。
    """

    app_key = str(app.app_key or "").strip()
    if app_key not in SUPPLIER_ORDER_APP_KEYS:
        return True
    if event_type not in {"message.received", "file.received"}:
        return True
    return _is_supplier_order_return_candidate(event_type=event_type, message=message, attachments=attachments)


def _is_supplier_order_return_candidate(
    *,
    event_type: str,
    message: Message,
    attachments: list[MessageAttachment],
) -> bool:
    """识别收发单回单候选消息。

    必须同时具备“子单ID/分单ID”和“运单号/快递单号”两个核心字段，并且文本中能看到
    真实运单号特征；文件消息优先读取已下载 Excel 附件内容，避免把普通聊天文件、图片或视频
    都推给收发单系统。
    """

    message_type = str(getattr(message, "message_type", "") or "").strip().lower()
    # 收发单系统不处理视频、图片、语音。即便这些媒体已经下载成附件，也不能推给它解析成回单文件。
    if message_type in {"image", "video", "voice"}:
        return False
    # file.received 是强文件事件，必须同时满足消息类型和附件类型都是 file，避免视频误入文件回单链路。
    if str(event_type or "").strip() == "file.received" and message_type != "file":
        return False

    content = str(message.content or "").strip()
    if _text_has_supplier_order_return_fields(content):
        return True
    for attachment in attachments:
        if _attachment_has_supplier_order_return_fields(attachment):
            return True
    return False


def _text_has_supplier_order_return_fields(text: str) -> bool:
    normalized = str(text or "").strip()
    if not (
        any(label in normalized for label in SUPPLIER_ORDER_SPLIT_LABELS)
        and any(label in normalized for label in SUPPLIER_ORDER_TRACKING_LABELS)
    ):
        return False
    labeled = re.search(r"(?:快递单号|运单号)\s*[:：]\s*([A-Za-z0-9][A-Za-z0-9\s-]{5,30})", normalized)
    if labeled and _looks_like_supplier_order_tracking_number(labeled.group(1)):
        return True
    for line in normalized.splitlines():
        row = line.strip()
        if not row or any(label in row for label in (*SUPPLIER_ORDER_SPLIT_LABELS, *SUPPLIER_ORDER_TRACKING_LABELS)):
            continue
        parts = [part for part in re.split(r"[\s,，|;；:：]+", row) if part]
        if len(parts) >= 3 and _looks_like_supplier_order_tracking_number(parts[2]):
            return True
    # HTML xls/xlsx XML 里字段名和值经常不在同一行，前面已经确认字段名同时存在；
    # 这里再扫描全文 token，只要能找到真实运单号，就允许投递给收发单系统。
    return any(_looks_like_supplier_order_tracking_number(token) for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]{7,30}", normalized))


def _attachment_has_supplier_order_return_fields(attachment: MessageAttachment) -> bool:
    if str(getattr(attachment, "attachment_type", "") or "").strip().lower() != "file":
        return False
    file_name = _extract_attachment_file_name(attachment)
    storage_path = str(attachment.storage_path or "").strip()
    suffix_source = file_name or storage_path
    # 收发单回单只接受 Excel 文件；视频/图片缓存、无后缀临时文件和其他办公文件都不进入解析。
    if not str(suffix_source or "").strip().lower().endswith(SUPPLIER_ORDER_EXCEL_SUFFIXES):
        return False
    storage_path = str(attachment.storage_path or "").strip()
    if not storage_path:
        return False
    path = Path(storage_path)
    if not path.exists() or not path.is_file():
        return False
    text = _read_attachment_text_for_supplier_order_filter(path)
    return _text_has_supplier_order_return_fields(text)


def _extract_attachment_file_name(attachment: MessageAttachment) -> str:
    metadata = _parse_json(getattr(attachment, "metadata_json", None))
    details = metadata.get("details") if isinstance(metadata.get("details"), dict) else {}
    return (
        str(metadata.get("file_name") or "").strip()
        or str(details.get("file_name") or details.get("filename") or "").strip()
    )


def _read_attachment_text_for_supplier_order_filter(path: Path) -> str:
    """读取附件中的少量文本用于字段识别。

    插件导出的 `.xls` 本质是 HTML 文本；`.xlsx` 则读取压缩包里的 XML。这里只做轻量字段探测，
    不做完整 Excel 解析，避免开放 webhook 投递阶段引入重业务处理。
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw[:2] == b"PK":
        return _read_xlsx_text_for_supplier_order_filter(path)
    sample = raw[: 1024 * 1024]
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return sample.decode(encoding)
        except UnicodeDecodeError:
            continue
    return sample.decode("utf-8", errors="ignore")


def _read_xlsx_text_for_supplier_order_filter(path: Path) -> str:
    try:
        with ZipFile(path) as archive:
            chunks: list[str] = []
            for name in archive.namelist():
                if not (name.startswith("xl/sharedStrings") or name.startswith("xl/worksheets/")):
                    continue
                chunks.append(archive.read(name).decode("utf-8", errors="ignore"))
                if sum(len(chunk) for chunk in chunks) > 1024 * 1024:
                    break
            return "\n".join(chunks)
    except (BadZipFile, OSError, KeyError):
        return ""


def _looks_like_supplier_order_tracking_number(value: str) -> bool:
    token = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return bool(re.fullmatch(r"[A-Z0-9]{8,24}", token) and any(char.isdigit() for char in token))
