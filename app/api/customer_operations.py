from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from app.core.db import SessionLocal
from app.models.entities import Channel
from app.services.customer_operations import submit_customer_operation
from app.services.crud import get_channel
from app.services.ops import record_audit


router = APIRouter(tags=["customer-operations"])


@router.post("/api/workwechat/customer-operations")
async def workwechat_customer_operation_action(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be object")

    db = SessionLocal()
    try:
        channel_id = _coerce_int(body.get("channel_id"))
        if not channel_id:
            raise HTTPException(status_code=400, detail="channel_id is required")
        channel = get_channel(db, channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail="Channel not found")
        _assert_customer_operation_authorized(channel, request)

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
    finally:
        db.close()


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
