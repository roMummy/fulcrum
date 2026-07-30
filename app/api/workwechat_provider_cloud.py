from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob
from app.services.channel_events import ingest_inbound_event
from app.services.inbound_files import build_inbound_file_download_url, store_inbound_upload
from app.services.ops import record_audit, record_health_check
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, WORKWECHAT_CLOUD_OUTBOUND_JOB, parse_job_payload
from app.services.workwechat_cloud_poll import (
    claim_cloud_jobs,
    complete_cloud_job,
    exchange_authorization_code,
    fail_cloud_job,
    find_channel_by_authorization_code,
    find_channel_by_client_token,
    load_cloud_poll_config,
)
from app.services.workwechat_ws_sessions import workwechat_ws_registry


router = APIRouter(prefix="/api/workwechat/provider", tags=["workwechat-provider-cloud"])


@router.post("/register")
async def register_workwechat_provider(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be object")
    authorization_code = str(body.get("authorization_code") or "").strip()
    if not authorization_code:
        raise HTTPException(status_code=400, detail="authorization_code_required")

    db = SessionLocal()
    try:
        channel = find_channel_by_authorization_code(db, authorization_code)
        if not channel:
            raise HTTPException(status_code=401, detail="authorization_code_invalid")
        try:
            result = exchange_authorization_code(
                channel,
                authorization_code,
                provider_name=str(body.get("provider_name") or "").strip(),
                client_version=str(body.get("client_version") or "").strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        db.commit()
        base_url = str(request.base_url).rstrip("/")
        return {
            "ok": True,
            "channel_id": channel.id,
            "client_token": result["client_token"],
            "inbound_mode": "client_token",
            "inbound_url": f"{base_url}/api/workwechat/provider/inbound",
            "inbound_upload_url": f"{base_url}/api/workwechat/provider/inbound-upload",
            # 保留兼容字段给旧客户端读取，但云端模式不再使用 callback_secret 做 HMAC 签名。
            "callback_url": "",
            "callback_secret": "",
            "poll_interval_seconds": result["poll_interval_seconds"],
            "poll_batch_size": result["poll_batch_size"],
        }
    finally:
        db.close()


@router.post("/poll")
async def poll_workwechat_provider_jobs(request: Request):
    channel = _load_channel_by_request_token(request)
    config = load_cloud_poll_config(channel)
    body = await _read_json_object(request)
    batch_size = int(body.get("batch_size") or config.get("poll_batch_size") or 5)
    db = SessionLocal()
    try:
        managed_channel = db.get(Channel, channel.id)
        if not managed_channel:
            raise HTTPException(status_code=404, detail="channel_not_found")
        jobs = claim_cloud_jobs(db, managed_channel, batch_size=batch_size)
    finally:
        db.close()
    return {
        "ok": True,
        "channel_id": channel.id,
        "jobs": jobs,
        "poll_interval_seconds": int(config.get("poll_interval_seconds") or 3),
    }


@router.post("/jobs/{job_id}/complete")
async def complete_workwechat_provider_job(job_id: int, request: Request):
    channel = _load_channel_by_request_token(request)
    body = await _read_json_object(request)
    db = SessionLocal()
    try:
        managed_channel = db.get(Channel, channel.id)
        if not managed_channel:
            raise HTTPException(status_code=404, detail="channel_not_found")
        return complete_cloud_job(db, managed_channel, job_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        db.close()


@router.post("/jobs/{job_id}/fail")
async def fail_workwechat_provider_job(job_id: int, request: Request):
    channel = _load_channel_by_request_token(request)
    body = await _read_json_object(request)
    db = SessionLocal()
    try:
        managed_channel = db.get(Channel, channel.id)
        if not managed_channel:
            raise HTTPException(status_code=404, detail="channel_not_found")
        return fail_cloud_job(
            db,
            managed_channel,
            job_id,
            error_code=str(body.get("error_code") or "cloud_job_failed"),
            error_message=str(body.get("error_message") or body.get("error") or "cloud_job_failed"),
            adapter_result=body.get("adapter_result") if isinstance(body.get("adapter_result"), dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        db.close()


@router.post("/inbound")
async def receive_workwechat_provider_inbound(request: Request):
    body = await _read_json_object(request)
    if not body:
        raise HTTPException(status_code=400, detail="request body must be object")
    db = SessionLocal()
    try:
        channel = _load_managed_channel_by_request_token(db, request)
        # 云端 provider 入站使用 client_token 绑定渠道，不再依赖渠道 Bot Token 或传统 webhook 签名。
        result = ingest_inbound_event(
            db,
            channel,
            body,
            headers={key: value for key, value in request.headers.items()},
            source="workwechat_provider_cloud",
        )
        normalized = result["normalized"]
        event_key = str(normalized.get("event_key") or "")
        if result["deduplicated"]:
            record_audit(
                db,
                "workwechat_provider_cloud.deduplicated",
                "raw_webhook_event",
                result["raw_event_id"],
                f"event_key={event_key}",
                actor="workwechat-provider",
            )
            return {"ok": True, "deduplicated": True, "event_id": result["raw_event_id"]}

        db.commit()
        record_health_check(
            db,
            "workwechat-provider-cloud-inbound",
            "channel",
            "ok",
            "企微云端 Provider 入站消息已接收并处理",
            target_id=channel.id,
            details={
                "event_key": event_key or "-",
                "mapped": result["mapped"],
                "event_type": normalized.get("event_type"),
                "adapter_details": normalized.get("details"),
                "dispatch": result.get("dispatch"),
            },
        )
        record_audit(
            db,
            "workwechat_provider_cloud.accept",
            "raw_webhook_event",
            result["raw_event_id"],
            f"mapped={result['mapped']}",
            actor="workwechat-provider",
        )
        return {
            "ok": True,
            "event_id": result["raw_event_id"],
            "mapped": result["mapped"],
            "event_type": normalized.get("event_type"),
            "dispatch": result.get("dispatch"),
        }
    finally:
        db.close()


@router.post("/inbound-upload")
async def upload_workwechat_provider_inbound_file(request: Request):
    body = await _read_json_object(request)
    if not body:
        raise HTTPException(status_code=400, detail="request body must be object")
    db = SessionLocal()
    try:
        channel = _load_managed_channel_by_request_token(db, request)
        file_name = str(body.get("file_name") or body.get("name") or "upload.bin").strip() or "upload.bin"
        content_base64 = str(body.get("content_base64") or "").strip()
        if not content_base64:
            raise HTTPException(status_code=400, detail="content_base64 is required")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid_content_base64: {exc}") from exc
        if not content:
            raise HTTPException(status_code=400, detail="empty file")

        # 文件内容仍落到主服务统一的 inbound_files 目录，后续插件读取路径和传统 webhook 模式保持一致。
        stored = store_inbound_upload(
            channel_id=channel.id,
            file_name=file_name,
            content=content,
            content_type=str(body.get("content_type") or "").strip() or None,
            message_type=str(body.get("message_type") or "file").strip() or "file",
            source_event_key=str(body.get("source_event_key") or body.get("event_key") or "").strip() or None,
        )
        download_url = build_inbound_file_download_url(
            request,
            channel_id=channel.id,
            share_id=stored["share_id"],
            file_name=quote(stored["file_name"]),
        )
        return {
            "ok": True,
            "channel_id": channel.id,
            "message_type": body.get("message_type") or "file",
            "file_name": stored["file_name"],
            "file_size": stored["file_size"],
            "content_type": stored["content_type"],
            "share_id": stored["share_id"],
            "storage_path": str(stored["target_path"]),
            "download_url": download_url,
        }
    finally:
        db.close()


@router.websocket("/ws")
async def workwechat_provider_ws(websocket: WebSocket):
    client_token = str(websocket.query_params.get("client_token") or "").strip()
    db = SessionLocal()
    try:
        channel = find_channel_by_client_token(db, client_token)
        if not channel:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        workwechat_ws_registry.register(channel.id, websocket, loop=asyncio.get_running_loop())
        pending_job_id = _find_pending_cloud_job_id(db, channel_id=int(channel.id or 0))
        if pending_job_id:
            # 兼容尚未升级的 provider：客户端重连后不一定主动 poll，服务端注册时补推一次唤醒。
            await websocket.send_text(json.dumps({"event_type": "job_available", "job_id": pending_job_id}, ensure_ascii=False))
    finally:
        db.close()
    try:
        while True:
            # 客户端可以发 heartbeat/status，当前版本只保持连接；后续再记录实时状态。
            await websocket.receive_text()
    except WebSocketDisconnect:
        workwechat_ws_registry.unregister(channel.id, websocket)


def _load_channel_by_request_token(request: Request):
    db = SessionLocal()
    try:
        return _load_managed_channel_by_request_token(db, request)
    finally:
        db.close()


def _load_managed_channel_by_request_token(db, request: Request):
    token = str(request.headers.get("X-WorkWechat-Client-Token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="client_token_required")
    channel = find_channel_by_client_token(db, token)
    if not channel:
        raise HTTPException(status_code=401, detail="invalid_client_token")
    return channel


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _find_pending_cloud_job_id(db, *, channel_id: int) -> int | None:
    """查找指定企微云端通道当前最早可领取的任务。

    WS 只负责唤醒，真正的领取仍走 /poll；这里只返回一个 job_id 作为轻量信号，
    避免 provider 重连时错过断线期间入队的任务。
    """

    jobs = db.scalars(
        select(TaskJob)
        .where(
            TaskJob.job_type.in_((WORKWECHAT_CLOUD_OUTBOUND_JOB, WORKWECHAT_CLOUD_CONTROL_JOB)),
            TaskJob.status == "pending",
            TaskJob.available_at <= now_shanghai(),
        )
        .order_by(TaskJob.priority.desc(), TaskJob.id.asc())
        .limit(100)
    ).all()
    for job in jobs:
        payload = parse_job_payload(job)
        if int(payload.get("channel_id") or 0) == int(channel_id or 0):
            return int(job.id or 0)
    return None
