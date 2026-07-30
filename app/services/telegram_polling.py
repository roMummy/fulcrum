from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import now_shanghai
from app.models.entities import Channel
from app.services.channel_adapters.telegram import TelegramAdapter
from app.services.channel_events import ingest_inbound_event


logger = get_logger(__name__)
POLL_BATCH_LIMIT = 20


def poll_telegram_channel(db: Session, channel: Channel) -> dict[str, Any]:
    adapter = TelegramAdapter()
    runtime_info = _parse_runtime_info(channel.runtime_info_json)
    telegram_runtime = runtime_info.get("telegram_polling")
    if not isinstance(telegram_runtime, dict):
        telegram_runtime = {}

    offset = _coerce_int(telegram_runtime.get("next_update_offset"))
    poll_at = now_shanghai()
    try:
        response = adapter.fetch_updates(channel, offset=offset, limit=POLL_BATCH_LIMIT, timeout=0)
    except Exception as exc:
        error_message = str(exc)
        telegram_runtime.update(
            {
                "last_poll_at": poll_at.isoformat(),
                "last_poll_ok": False,
                "last_error": error_message,
            }
        )
        runtime_info["telegram_polling"] = telegram_runtime
        channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
        channel.status = "degraded"
        db.commit()
        logger.warning(
            "telegram polling failed",
            extra={"channel_id": channel.id, "error": error_message},
        )
        return {"ok": False, "polled": False, "processed": 0, "error": error_message}

    if not response.get("ok"):
        error_message = str(response.get("description") or "telegram getUpdates failed")
        telegram_runtime.update(
            {
                "last_poll_at": poll_at.isoformat(),
                "last_poll_ok": False,
                "last_error": error_message,
            }
        )
        runtime_info["telegram_polling"] = telegram_runtime
        channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
        channel.status = "degraded"
        db.commit()
        return {"ok": False, "polled": True, "processed": 0, "error": error_message}

    updates = response.get("result")
    items = updates if isinstance(updates, list) else []
    processed = 0
    last_update_id: int | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        result = ingest_inbound_event(
            db,
            channel,
            item,
            headers={"X-Telegram-Polling": "true"},
            source="telegram_poll",
        )
        processed += 1
        update_id = _coerce_int(item.get("update_id"))
        if update_id is not None:
            last_update_id = update_id
        logger.info(
            "telegram update polled",
            extra={
                "channel_id": channel.id,
                "raw_event_id": result.get("raw_event_id") or 0,
                "deduplicated": bool(result.get("deduplicated")),
            },
        )

    if last_update_id is not None:
        telegram_runtime["last_update_id"] = last_update_id
        telegram_runtime["next_update_offset"] = last_update_id + 1
    telegram_runtime["last_poll_at"] = poll_at.isoformat()
    telegram_runtime["last_poll_ok"] = True
    telegram_runtime["last_error"] = None
    telegram_runtime["last_batch_size"] = len(items)
    telegram_runtime["receive_total"] = int(telegram_runtime.get("receive_total") or 0) + processed
    runtime_info["telegram_polling"] = telegram_runtime
    channel.runtime_info_json = json.dumps(runtime_info, ensure_ascii=False, sort_keys=True)
    channel.last_message_sync_at = poll_at
    if channel.is_enabled:
        channel.status = "online"
    db.commit()
    return {
        "ok": True,
        "polled": True,
        "processed": processed,
        "batch_size": len(items),
        "next_update_offset": telegram_runtime.get("next_update_offset"),
    }


def _parse_runtime_info(raw: str | None) -> dict[str, Any]:
    normalized = (raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
