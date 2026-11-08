"""第三方支付成功回调入口。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, HTTPException, Request

from app.core.db import SessionLocal
from app.core.settings import settings
from app.services.billing import BillingError, auto_fulfill_paid_order


router = APIRouter(prefix="/api/payment", tags=["payment-webhooks"])
PAYMENT_TIMESTAMP_TOLERANCE_SECONDS = 300


def sign_payment_webhook(secret: str, raw_body: bytes, timestamp: str) -> str:
    """生成支付回调签名，签名原文与开放 API 保持相同换行规则。"""

    return hmac.new(
        str(secret or "").encode("utf-8"),
        str(timestamp or "").encode("utf-8") + b"\n" + raw_body,
        hashlib.sha256,
    ).hexdigest()


@router.post("/webhook")
async def payment_webhook(request: Request):
    """校验支付渠道签名、订单金额并自动开通订阅。"""

    secret = str(settings.payment_webhook_secret or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="payment_webhook_not_configured")
    raw_body = await request.body()
    timestamp = str(request.headers.get("X-Payment-Timestamp") or "").strip()
    signature = str(request.headers.get("X-Payment-Signature") or "").strip()
    try:
        timestamp_value = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid_payment_timestamp") from exc
    if abs(int(time.time()) - timestamp_value) > PAYMENT_TIMESTAMP_TOLERANCE_SECONDS:
        raise HTTPException(status_code=401, detail="payment_timestamp_expired")
    expected = sign_payment_webhook(secret, raw_body, timestamp)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid_payment_signature")
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid_payment_json") from exc
    if not isinstance(payload, dict) or str(payload.get("status") or "paid").strip().lower() not in {"paid", "success", "succeeded"}:
        raise HTTPException(status_code=400, detail="payment_not_successful")
    try:
        amount_cents = int(payload.get("amount_cents") or 0)
        with SessionLocal() as db:
            order = auto_fulfill_paid_order(
                db,
                order_no=str(payload.get("order_no") or ""),
                amount_cents=amount_cents,
                provider_transaction_id=str(payload.get("transaction_id") or payload.get("payment_reference") or ""),
            )
    except BillingError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "order_no": order.order_no, "status": order.status, "subscription_id": order.fulfilled_subscription_id}

