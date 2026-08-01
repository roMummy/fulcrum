from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from app.core.db import SessionLocal, TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.models.entities import Channel
from app.services.customer_operations import submit_customer_operation
from app.services.ops import record_audit


router = APIRouter(tags=["customer-operations"])


@router.post("/api/workwechat/customer-operations")
async def workwechat_customer_operation_action(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be object")

    channel_id = _coerce_int(body.get("channel_id"))
    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id is required")

    # 第一阶段只按数据库中的渠道密钥认证机器身份。请求正文中的 tenant_id 即使
    # 存在也完全忽略，租户归属只能来自认证成功的渠道记录。
    with platform_session(SessionLocal) as auth_db:
        channel = auth_db.get(Channel, channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        _assert_customer_operation_authorized(channel, request)
        credential = (int(channel.tenant_id), int(channel.id))

    with tenant_scope(credential[0]), tenant_session(TenantSessionLocal) as db:
        channel = db.get(Channel, credential[1])
        if channel is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        try:
            result = submit_customer_operation(db, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.commit()
        record_audit(
            db,
            "workwechat.customer-operation.accept",
            "customer_operation_request",
            int(result.get("operation_id") or 0),
            f"request_id={result.get('request_id')}, status={result.get('status')}",
        )
        return result


def _assert_customer_operation_authorized(channel: Channel, request: Request) -> None:
    expected = _load_customer_operation_token(channel)
    if not expected:
        raise HTTPException(status_code=503, detail="customer_operation_token_not_configured")
    provided = str(request.headers.get("X-Customer-Operation-Token") or "").strip()
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid_customer_operation_token")


def _load_customer_operation_token(channel: Channel) -> str:
    raw = str(channel.adapter_config_json or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    headers = parsed.get("headers") if isinstance(parsed.get("headers"), dict) else {}
    return str(
        parsed.get("customer_operation_token")
        or headers.get("X-Customer-Operation-Token")
        or headers.get("x-customer-operation-token")
        or ""
    ).strip()


def _coerce_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
