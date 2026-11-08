from __future__ import annotations

from app.services.open_event_catalog import OPEN_EVENT_TYPES, build_open_event_id, is_open_event_type


def test_open_event_catalog_is_stable_and_ids_are_deterministic() -> None:
    assert "message.received" in OPEN_EVENT_TYPES
    assert "job.failed" in OPEN_EVENT_TYPES
    assert is_open_event_type("message.sent")
    assert not is_open_event_type("login.qr")
    assert build_open_event_id(event_type="message.received", scope_id=3, sequence=9) == "evt_message.received_3_9"
