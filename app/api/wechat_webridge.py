from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_db import platform_session, tenant_session
from app.core.tenant_context import tenant_scope
from app.models.entities import Channel
from app.services.channel_events import ingest_inbound_event
from app.services.saas_auth import has_tenant_role
from app.services.wechat_webridge.service import (
    accept_command_result,
    accept_state,
    claim_commands,
    mark_event_seen,
    register_device,
    enqueue_command,
    get_command_result,
    command_catalog,
    list_command_results,
    identity_matches,
)
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import wakeup
from app.services.account_connections import verify_account_connection
from app.services.wechat_webridge.access import verify_connection_token
from app.services.account_connections.contracts import AccountConnectionError
from app.models.entities import MessageAttachment, MomentsMedia
from app.services import moments_media
from app.services.inbound_media import store_uploaded_media_attachment
from app.core.logging import get_logger

router = APIRouter(prefix="/api/wechat-webridge", tags=["wechat-webridge"])
logger = get_logger(__name__)

# 长轮询挂起时长需小于手机插件的 HTTP 超时；队列检查间隔决定新命令的最大下发延迟。
POLL_HOLD_SECONDS = 15
POLL_CHECK_INTERVAL_SECONDS = 1


def _payload_summary(payload: dict) -> dict[str, object]:
    """只记录字段形状和关键标识，调试日志不落设备密钥、Token 或消息正文。"""

    summary: dict[str, object] = {"keys": sorted(str(key) for key in payload.keys())}
    for key in ("request_id", "command_name", "type", "event_id", "device_id", "account_id"):
        value = payload.get(key)
        if value not in (None, ""):
            text = str(value)
            summary[key] = text[:120] if key not in {"device_id", "account_id"} else f"{text[:8]}..."
    return summary


def _channel_tenant(channel_id: int) -> tuple[int, int]:
    """只通过平台会话解析渠道租户，避免请求正文决定租户归属。"""

    with platform_session(SessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        # 已逻辑删除的账号不再接受手机的事件、轮询和命令回报。
        if channel is None or channel.channel_type != "wechat_webridge" or channel.deleted_at is not None:
            raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
        return int(channel.tenant_id), int(channel.id)


def _load_channel(channel_id: int):
    tenant_id, actual_id = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, actual_id)
        if channel is None or channel.channel_type != "wechat_webridge":
            raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
        return tenant_id, channel


async def _json(request: Request) -> dict:
    """严格要求 JSON 对象，给插件返回可重试的 4xx。"""

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request_body_must_be_object")
    return body


def _require_device_token(channel_id: int, connection_token: str) -> None:
    """所有手机入口在读取请求正文前验证渠道专属 Token。"""

    _channel_tenant(channel_id)
    with platform_session(SessionLocal) as db:
        channel = db.get(Channel, channel_id)
        if not verify_connection_token(channel, connection_token):
            raise HTTPException(status_code=403, detail="微桥连接地址无效，请重新复制接入地址")


@router.get("/{channel_id}/capabilities")
async def capabilities(channel_id: int, request: Request):
    """返回可直接用于控制台和集成方的微桥命令目录。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        return {"ok": True, "channel_id": channel_id, **command_catalog(channel)}


@router.post("/{channel_id}/{connection_token}/agent/register", dependencies=[Depends(_require_device_token)])
async def register(channel_id: int, request: Request):
    body = await _json(request)
    logger.info("webridge register received", extra={"channel_id": channel_id, "payload": _payload_summary(body)})
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.scalar(select(Channel).where(Channel.id == channel_id).with_for_update())
        try:
            result = register_device(db, channel, body)
            if channel.is_enabled or (channel.entitlement_binding is not None and channel.entitlement_binding.status in {"active", "disabled"}):
                return {**result, "connection_state": "active", "message": "微信账号已连接"}
            # 首次注册只准备身份信息，额度绑定和启用必须由网页登录用户确认。
            verified = verify_account_connection(db, channel_id=int(channel.id))
            return {**result, "connection_state": verified.state, "message": "请在网页核对微信账号信息并确认接入"}
        except AccountConnectionError as exc:
            # 注册本身已经成功时，保留设备快照并返回可读状态，手机端仍可继续心跳和轮询。
            logger.exception("webridge register binding failed", extra={"channel_id": channel_id, "error_code": str(exc)})
            return {"ok": True, "channel_id": int(channel.id), "connection_state": "pending", "message": str(exc)}
        except ValueError as exc:
            logger.exception("webridge register rejected", extra={"channel_id": channel_id, "error_code": str(exc)})
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{channel_id}/{connection_token}/agent/state", dependencies=[Depends(_require_device_token)])
async def state(channel_id: int, request: Request):
    body = await _json(request)
    logger.info("webridge state received", extra={"channel_id": channel_id, "payload": _payload_summary(body)})
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        try:
            return accept_state(db, db.get(Channel, int(channel_id)), body)
        except ValueError as exc:
            logger.exception("webridge state rejected", extra={"channel_id": channel_id, "error_code": str(exc)})
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/{connection_token}/agent/event", dependencies=[Depends(_require_device_token)])
async def event(channel_id: int, request: Request):
    body = await _json(request)
    logger.info("webridge event received", extra={"channel_id": channel_id, "payload": _payload_summary(body)})
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        if not channel:
            raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
        if not identity_matches(channel, body):
            raise HTTPException(status_code=409, detail="webridge_identity_mismatch")
        if not channel.is_enabled:
            return {"ok": True, "ignored": True, "message": "等待网页确认接入"}
        try:
            # 事件 ID 先写回顶层，适配器和统一事件服务都能使用同一个去重键。
            body.setdefault("event_id", mark_event_seen(channel, body))
            result = ingest_inbound_event(db, channel, body, headers=dict(request.headers), source="wechat_webridge")
            db.commit()
            return {"ok": True, "deduplicated": bool(result.get("deduplicated")), "event_id": body["event_id"]}
        except ValueError as exc:
            logger.exception("webridge event rejected", extra={"channel_id": channel_id, "error_code": str(exc)})
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _claim_once(channel_id: int, tenant_id: int, body: dict) -> list[dict]:
    """用独立短会话领取一次命令；等待期间不持有数据库连接。"""

    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        return claim_commands(db, db.get(Channel, int(channel_id)), body)


@router.post("/{channel_id}/{connection_token}/agent/poll", dependencies=[Depends(_require_device_token)])
async def poll(channel_id: int, request: Request):
    """长轮询：有命令立即返回，没有则最多挂起 POLL_HOLD_SECONDS 秒再返回空列表。

    手机插件拿到空响应会立刻重发请求，若服务端不挂起就会形成每秒近百次的紧循环。
    """

    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    deadline = time.monotonic() + POLL_HOLD_SECONDS
    # 挂起期间登记一个唤醒事件:新命令入队、或延迟命令到点时,入队方会立刻叫醒这里(见 wakeup),
    # 不必等下一次 1 秒检查;检查间隔仍保留作为兜底。
    wake = asyncio.Event()
    loop = asyncio.get_running_loop()
    wakeup.register(channel_id, loop, wake)
    try:
        while True:
            # 先清除再查询:查询之后、等待之前到来的通知不会丢(事件保持已触发状态,等待立刻返回)。
            wake.clear()
            try:
                commands = await run_in_threadpool(_claim_once, channel_id, tenant_id, body)
            except ValueError as exc:
                logger.exception("webridge poll rejected", extra={"channel_id": channel_id, "error_code": str(exc)})
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if commands or time.monotonic() >= deadline or await request.is_disconnected():
                return {"commands": commands}
            try:
                await asyncio.wait_for(wake.wait(), timeout=POLL_CHECK_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        wakeup.unregister(channel_id, loop, wake)


@router.post("/{channel_id}/{connection_token}/agent/command_result", dependencies=[Depends(_require_device_token)])
async def command_result(channel_id: int, request: Request):
    body = await _json(request)
    logger.info("webridge command result received", extra={"channel_id": channel_id, "payload": _payload_summary(body)})
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        try:
            return accept_command_result(db, db.get(Channel, int(channel_id)), body)
        except ValueError as exc:
            logger.exception("webridge command result rejected", extra={"channel_id": channel_id, "error_code": str(exc)})
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/{connection_token}/agent/upload_media", dependencies=[Depends(_require_device_token)])
async def upload_media(
    channel_id: int,
    attachment_id: int,
    file: UploadFile = File(...),
    account_id: str = Form(""),
    device_id: str = Form(""),
    session_id: str = Form(""),
):
    """接收手机执行 upload_message_media 后回传的媒体文件。"""

    # 微桥文档规定上传是 multipart/form-data,设备身份三元组在表单字段里,不在请求头。
    body = {"device_id": device_id, "session_id": session_id, "account_id": account_id}
    logger.info("webridge media upload received", extra={"channel_id": channel_id, "attachment_id": attachment_id, "upload_filename": file.filename or "", "content_type": file.content_type or ""})
    tenant_id, _ = _channel_tenant(channel_id)
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        try:
            if not identity_matches(channel, body):
                raise ValueError("webridge_identity_mismatch")
            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.id == int(attachment_id), MessageAttachment.channel_id == channel.id))
            if attachment is None:
                raise ValueError("webridge_attachment_not_found")
            result = store_uploaded_media_attachment(
                db,
                attachment=attachment,
                content=await file.read(),
                filename=file.filename or "",
                content_type=file.content_type or "application/octet-stream",
            )
            db.commit()
            # 微桥要求响应带非空 asset_id,缺少会判定上传失败;用附件主键作为资源 ID。
            return {"ok": True, "asset_id": str(result["attachment_id"]), **result}
        except ValueError as exc:
            logger.exception("webridge media upload rejected", extra={"channel_id": channel_id, "attachment_id": attachment_id, "error_code": str(exc)})
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{channel_id}/{connection_token}/agent/moments_upload", dependencies=[Depends(_require_device_token)])
async def moments_upload(channel_id: int, media_id: int, request: Request):
    """接收手机传回的朋友圈媒体(moments_media_download 的 upload 目标,请求体就是文件的原始字节)。

    这个地址不带登录态,所以靠请求时生成的一次性令牌(请求头 X-Upload-Token)证明身份;
    边收边写盘,超过大小上限立即拒绝;写完后再核对一次令牌并让它作废,保证同一个令牌只能成功一次。
    """

    tenant_id, actual_channel_id = _channel_tenant(channel_id)
    token = request.headers.get(moments_media.UPLOAD_HEADER, "")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > moments_media.MAX_BYTES:
        raise HTTPException(status_code=413, detail="moments_media_too_large")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        if not moments_media.accepts_upload(db.get(MomentsMedia, int(media_id)), token, actual_channel_id):
            raise HTTPException(status_code=403, detail="moments_upload_not_accepted")
    directory = moments_media.storage_dir(actual_channel_id)
    directory.mkdir(parents=True, exist_ok=True)
    partial = directory / f"{int(media_id)}.part"
    size, head = 0, b""
    try:
        with partial.open("wb") as handle:
            async for chunk in request.stream():
                if not chunk:
                    continue
                size += len(chunk)
                if size > moments_media.MAX_BYTES:
                    raise HTTPException(status_code=413, detail="moments_media_too_large")
                if len(head) < 16:
                    head += chunk[: 16 - len(head)]
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="moments_media_empty")
        mime_type, extension = moments_media.sniff(head, request.headers.get("content-type", ""))
        final = directory / f"{int(media_id)}{extension}"
        os.replace(partial, final)
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            row = db.get(MomentsMedia, int(media_id))
            if not moments_media.accepts_upload(row, token, actual_channel_id):
                final.unlink(missing_ok=True)
                raise HTTPException(status_code=409, detail="moments_upload_not_accepted")
            moments_media.finish_upload(row, final, size, mime_type)
            db.commit()
    finally:
        partial.unlink(missing_ok=True)
    logger.info("moments media uploaded", extra={"channel_id": actual_channel_id, "media_id": media_id, "file_size": size, "mime_type": mime_type})
    # 手机要求响应带非空 asset_id,用记录主键作为资源 ID。
    return {"ok": True, "asset_id": str(media_id), "file_size": size}


@router.post("/{channel_id}/commands")
async def enqueue_generic_command(channel_id: int, request: Request):
    """供已登录租户操作员提交 WeBridge 全量命令；手机侧仍只通过 poll 收取。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    body = await _json(request)
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        try:
            job = enqueue_command(
                db,
                channel,
                command_name=str(body.get("command_name") or body.get("type") or ""),
                args=body.get("args") if isinstance(body.get("args"), dict) else {},
                request_id=str(body.get("request_id") or ""),
            )
            db.commit()
            payload = parse_job_payload(job)
            return {"ok": True, "job_id": int(job.id), "request_id": payload.get("command", {}).get("request_id"), "status": job.status}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{channel_id}/commands/{request_id}")
async def command_result_query(channel_id: int, request_id: str, request: Request):
    """查询通用命令的排队、执行或失败结果。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        result = get_command_result(db, db.get(Channel, int(channel_id)), request_id)
        if result is None:
            raise HTTPException(status_code=404, detail="webridge_command_not_found")
        return {"ok": True, **result}


@router.get("/{channel_id}/commands")
async def command_results(channel_id: int, request: Request, status: str = "", command_type: str = "", limit: int = 50):
    """查询当前渠道最近的微桥命令执行记录。"""

    user = getattr(request.state, "current_user", None)
    if user is None or not has_tenant_role(user, "operator"):
        raise HTTPException(status_code=403, detail="operator_role_required")
    tenant_id, _ = _channel_tenant(channel_id)
    if int(getattr(user, "tenant_id", 0) or 0) != tenant_id:
        raise HTTPException(status_code=404, detail="wechat_webridge_channel_not_found")
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, int(channel_id))
        return {"ok": True, "channel_id": channel_id, "items": list_command_results(db, channel, status=status, command_type=command_type, limit=limit)}
