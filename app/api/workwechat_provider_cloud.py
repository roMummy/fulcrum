from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob
from app.models.saas import ProviderNodeAccount
from app.services.channel_events import ingest_inbound_event
from app.services.inbound_files import build_inbound_file_download_url, store_inbound_upload
from app.services.ops import record_audit, record_health_check
from app.services.provider_nodes import (
    ActivationCodeInvalid,
    DeviceCredentialInvalid,
    activate_provider_node,
    authenticate_device_token,
    provider_poll_allowed,
    record_provider_heartbeat,
)
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, WORKWECHAT_CLOUD_OUTBOUND_JOB, parse_job_payload
from app.services.workwechat_cloud_poll import (
    claim_cloud_jobs,
    claim_provider_node_jobs,
    complete_provider_node_job,
    complete_cloud_job,
    exchange_authorization_code,
    fail_cloud_job,
    fail_provider_node_job,
    find_channel_by_authorization_code,
    find_channel_by_client_token,
    load_cloud_poll_config,
)
from app.services.workwechat_ws_sessions import workwechat_ws_registry


router = APIRouter(prefix="/api/workwechat/provider", tags=["workwechat-provider-cloud"])
provider_router = APIRouter(prefix="/api/provider", tags=["provider-node-cloud"])


@provider_router.post("/activate")
async def activate_provider_device(request: Request):
    """使用一次性激活码换取节点设备密钥，不接受客户端指定租户或渠道。"""

    body = await _read_json_object(request)
    if "tenant_id" in body or "channel_id" in body:
        raise HTTPException(status_code=400, detail="tenant_or_channel_not_allowed")
    activation_code = str(body.get("activation_code") or "").strip()
    fingerprint = str(body.get("fingerprint") or body.get("device_fingerprint") or "").strip()
    if not activation_code or not fingerprint:
        raise HTTPException(status_code=400, detail="activation_code_and_fingerprint_required")
    with platform_session(SessionLocal) as db:
        try:
            activated = activate_provider_node(
                db,
                activation_code,
                fingerprint=fingerprint,
                client_version=str(body.get("client_version") or "").strip(),
            )
        except ActivationCodeInvalid as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    base_url = str(request.base_url).rstrip("/")
    return {
        "ok": True,
        "device_id": activated.device_id,
        "device_token": activated.raw_device_token,
        "heartbeat_url": f"{base_url}/api/provider/heartbeat",
        "poll_url": f"{base_url}/api/provider/poll",
        "poll_timeout_seconds": 25,
    }


@provider_router.post("/heartbeat")
async def provider_node_heartbeat(request: Request):
    """记录节点状态和账号摘要；心跳在租户到期后仍保持可用。"""

    identity = _load_device_identity(request)
    body = await _read_json_object(request)
    with tenant_scope(identity.tenant_id), tenant_session(TenantSessionLocal) as db:
        node = record_provider_heartbeat(
            db,
            node_id=identity.node_id,
            client_version=str(body.get("client_version") or "").strip(),
            hook_status=str(body.get("hook_status") or "").strip(),
            workwechat_status=str(body.get("workwechat_status") or body.get("process_status") or "").strip(),
            accounts=body.get("accounts") if isinstance(body.get("accounts"), list) else [],
            details=body.get("details") if isinstance(body.get("details"), dict) else {},
        )
        poll_allowed = provider_poll_allowed(db, identity.tenant_id)
    return {
        "ok": True,
        "device_id": identity.device_id,
        "node_id": identity.node_id,
        "server_time": now_shanghai().isoformat(),
        "poll_allowed": poll_allowed,
        "last_heartbeat_at": node.last_heartbeat_at.isoformat() if node.last_heartbeat_at else None,
    }


@provider_router.post("/poll")
async def poll_provider_node_jobs(request: Request):
    """一个设备密钥领取节点下多个企微账号任务。"""

    identity = _load_device_identity(request)
    body = await _read_json_object(request)
    with tenant_scope(identity.tenant_id), tenant_session(TenantSessionLocal) as db:
        if not provider_poll_allowed(db, identity.tenant_id):
            return {"ok": True, "jobs": [], "read_only": True, "poll_interval_seconds": 20}
        jobs = claim_provider_node_jobs(
            db,
            node_id=identity.node_id,
            batch_size=int(body.get("batch_size") or 5),
            lease_seconds=int(body.get("lease_seconds") or 900),
        )
    return {"ok": True, "jobs": jobs, "read_only": False, "poll_interval_seconds": 3}


@provider_router.post("/jobs/{job_id}/complete")
async def complete_provider_job(job_id: int, request: Request):
    """使用设备身份和任务租约完成任务。"""

    identity = _load_device_identity(request)
    body = await _read_json_object(request)
    with tenant_scope(identity.tenant_id), tenant_session(TenantSessionLocal) as db:
        try:
            return complete_provider_node_job(
                db,
                node_id=identity.node_id,
                job_id=job_id,
                lease_token=str(body.get("lease_token") or "").strip(),
                result_payload=body,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409 if str(exc) == "lease_expired" else 404, detail=str(exc)) from exc


@provider_router.post("/jobs/{job_id}/fail")
async def fail_provider_job(job_id: int, request: Request):
    """使用设备身份和任务租约回报失败。"""

    identity = _load_device_identity(request)
    body = await _read_json_object(request)
    with tenant_scope(identity.tenant_id), tenant_session(TenantSessionLocal) as db:
        try:
            return fail_provider_node_job(
                db,
                node_id=identity.node_id,
                job_id=job_id,
                lease_token=str(body.get("lease_token") or "").strip(),
                error_code=str(body.get("error_code") or "provider_job_failed"),
                error_message=str(body.get("error_message") or body.get("error") or "provider_job_failed"),
                adapter_result=body.get("adapter_result") if isinstance(body.get("adapter_result"), dict) else {},
            )
        except ValueError as exc:
            raise HTTPException(status_code=409 if str(exc) == "lease_expired" else 404, detail=str(exc)) from exc


@provider_router.post("/inbound")
async def receive_provider_node_inbound(request: Request):
    """按设备节点和稳定账号键解析已绑定渠道后接收入站事件。"""

    identity = _load_device_identity(request)
    body = await _read_json_object(request)
    if not body:
        raise HTTPException(status_code=400, detail="request body must be object")
    with tenant_scope(identity.tenant_id), tenant_session(TenantSessionLocal) as db:
        if not provider_poll_allowed(db, identity.tenant_id):
            raise HTTPException(status_code=403, detail="tenant_read_only")
        channel = _load_channel_for_node_account(db, node_id=identity.node_id, body=body)
        result = ingest_inbound_event(
            db,
            channel,
            body,
            headers={key: value for key, value in request.headers.items()},
            source="provider_node_cloud",
        )
        normalized = result["normalized"]
        return {
            "ok": True,
            "deduplicated": result["deduplicated"],
            "event_id": result["raw_event_id"],
            "mapped": result["mapped"],
            "event_type": normalized.get("event_type"),
            "dispatch": result.get("dispatch"),
        }


@provider_router.post("/inbound-upload")
async def upload_provider_node_inbound_file(request: Request):
    """按节点账号绑定保存入站文件，客户端不能直接指定渠道主键。"""

    identity = _load_device_identity(request)
    body = await _read_json_object(request)
    if not body:
        raise HTTPException(status_code=400, detail="request body must be object")
    with tenant_scope(identity.tenant_id), tenant_session(TenantSessionLocal) as db:
        if not provider_poll_allowed(db, identity.tenant_id):
            raise HTTPException(status_code=403, detail="tenant_read_only")
        channel = _load_channel_for_node_account(db, node_id=identity.node_id, body=body)
        content_base64 = str(body.get("content_base64") or "").strip()
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid_content_base64") from exc
        if not content:
            raise HTTPException(status_code=400, detail="empty file")
        stored = store_inbound_upload(
            tenant_id=channel.tenant_id,
            channel_id=channel.id,
            file_name=str(body.get("file_name") or body.get("name") or "upload.bin").strip() or "upload.bin",
            content=content,
            content_type=str(body.get("content_type") or "").strip() or None,
            message_type=str(body.get("message_type") or "file").strip() or "file",
            source_event_key=str(body.get("source_event_key") or body.get("event_key") or "").strip() or None,
        )
        return {
            "ok": True,
            "provider_account_key": str(body.get("provider_account_key") or "").strip(),
            "file_name": stored["file_name"],
            "file_size": stored["file_size"],
            "content_type": stored["content_type"],
            "share_id": stored["share_id"],
            "download_url": build_inbound_file_download_url(
                request,
                channel_id=channel.id,
                share_id=stored["share_id"],
                file_name=quote(stored["file_name"]),
            ),
        }


@router.post("/register")
async def register_workwechat_provider(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be object")
    authorization_code = str(body.get("authorization_code") or "").strip()
    if not authorization_code:
        raise HTTPException(status_code=400, detail="authorization_code_required")

    with platform_session(SessionLocal) as auth_db:
        channel = find_channel_by_authorization_code(auth_db, authorization_code)
        if not channel:
            raise HTTPException(status_code=401, detail="authorization_code_invalid")
        credential = (int(channel.tenant_id), int(channel.id))

    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, credential[1])
        if channel is None:
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


@router.post("/poll")
async def poll_workwechat_provider_jobs(request: Request):
    credential = _load_channel_credential_by_request_token(request)
    body = await _read_json_object(request)
    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        managed_channel = db.get(Channel, credential[1])
        if not managed_channel:
            raise HTTPException(status_code=404, detail="channel_not_found")
        config = load_cloud_poll_config(managed_channel)
        batch_size = int(body.get("batch_size") or config.get("poll_batch_size") or 5)
        jobs = claim_cloud_jobs(db, managed_channel, batch_size=batch_size)
    return {
        "ok": True,
        "channel_id": credential[1],
        "jobs": jobs,
        "poll_interval_seconds": int(config.get("poll_interval_seconds") or 3),
    }


@router.post("/jobs/{job_id}/complete")
async def complete_workwechat_provider_job(job_id: int, request: Request):
    credential = _load_channel_credential_by_request_token(request)
    body = await _read_json_object(request)
    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        managed_channel = db.get(Channel, credential[1])
        if not managed_channel:
            raise HTTPException(status_code=404, detail="channel_not_found")
        try:
            return complete_cloud_job(db, managed_channel, job_id, body)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/fail")
async def fail_workwechat_provider_job(job_id: int, request: Request):
    credential = _load_channel_credential_by_request_token(request)
    body = await _read_json_object(request)
    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        managed_channel = db.get(Channel, credential[1])
        if not managed_channel:
            raise HTTPException(status_code=404, detail="channel_not_found")
        try:
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


@router.post("/inbound")
async def receive_workwechat_provider_inbound(request: Request):
    body = await _read_json_object(request)
    if not body:
        raise HTTPException(status_code=400, detail="request body must be object")
    credential = _load_channel_credential_by_request_token(request)
    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, credential[1])
        if channel is None:
            raise HTTPException(status_code=401, detail="invalid_client_token")
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


@router.post("/inbound-upload")
async def upload_workwechat_provider_inbound_file(request: Request):
    body = await _read_json_object(request)
    if not body:
        raise HTTPException(status_code=400, detail="request body must be object")
    credential = _load_channel_credential_by_request_token(request)
    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, credential[1])
        if channel is None:
            raise HTTPException(status_code=401, detail="invalid_client_token")
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
            tenant_id=channel.tenant_id,
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


@router.websocket("/ws")
async def workwechat_provider_ws(websocket: WebSocket):
    client_token = str(websocket.query_params.get("client_token") or "").strip()
    credential = _find_channel_credential_by_client_token(client_token)
    if credential is None:
        await websocket.close(code=1008)
        return
    channel_id = credential[1]
    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, channel_id)
        if channel is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        workwechat_ws_registry.register(channel_id, websocket, loop=asyncio.get_running_loop())
        pending_job_id = _find_pending_cloud_job_id(db, channel_id=channel_id)
        if pending_job_id:
            # 兼容尚未升级的 provider：客户端重连后不一定主动 poll，服务端注册时补推一次唤醒。
            await websocket.send_text(json.dumps({"event_type": "job_available", "job_id": pending_job_id}, ensure_ascii=False))
    try:
        while True:
            # 客户端可以发 heartbeat/status，当前版本只保持连接；后续再记录实时状态。
            await websocket.receive_text()
    except WebSocketDisconnect:
        workwechat_ws_registry.unregister(channel_id, websocket)


def _load_channel_credential_by_request_token(request: Request) -> tuple[int, int]:
    token = str(request.headers.get("X-WorkWechat-Client-Token") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="client_token_required")
    credential = _find_channel_credential_by_client_token(token)
    if credential is None:
        raise HTTPException(status_code=401, detail="invalid_client_token")
    return credential


def _find_channel_credential_by_client_token(token: str) -> tuple[int, int] | None:
    """在认证阶段把 client token 收敛为可信租户和渠道主键。"""

    with platform_session(SessionLocal) as auth_db:
        channel = find_channel_by_client_token(auth_db, token)
        if channel is None:
            return None
        return int(channel.tenant_id), int(channel.id)


def _load_device_identity(request: Request):
    """从请求头设备密钥恢复最小可信节点身份。"""

    raw_token = str(request.headers.get("X-Provider-Device-Token") or "").strip()
    if not raw_token:
        authorization = str(request.headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            raw_token = authorization[7:].strip()
    with platform_session(SessionLocal) as db:
        try:
            return authenticate_device_token(db, raw_token)
        except DeviceCredentialInvalid as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc


def _load_channel_for_node_account(db, *, node_id: int, body: dict[str, Any]) -> Channel:
    """通过节点内稳定账号键解析唯一已绑定企微渠道。"""

    account_key = str(body.get("provider_account_key") or body.get("account_key") or "").strip()
    if not account_key:
        raise HTTPException(status_code=400, detail="provider_account_key_required")
    account = db.scalar(
        select(ProviderNodeAccount).where(
            ProviderNodeAccount.provider_node_id == int(node_id),
            ProviderNodeAccount.account_key == account_key,
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="provider_account_not_found")
    channel = db.scalar(
        select(Channel).where(
            Channel.channel_type == "workwechat",
            Channel.provider_node_id == int(node_id),
            Channel.provider_account_id == account.id,
        )
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="provider_account_channel_not_bound")
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
