from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.core.time import now_shanghai
from app.models.entities import Channel
from app.models.saas import ProviderNode, ProviderNodeAccount
from app.services.account_presence import build_account_presence


@pytest.mark.parametrize("channel_status", ["online", "degraded"])
def test_webridge_uses_phone_poll_timestamp_and_expires_stale_online_flag(channel_status) -> None:
    now = now_shanghai()
    channel = Channel(channel_type="wechat_webridge", is_enabled=True, status=channel_status)
    for age, expected in ((30, "online"), (120, "online"), (121, "offline")):
        seen = now - timedelta(seconds=age)
        channel.runtime_info_json = json.dumps({"webridge": {"online": True, "last_seen_at": seen.isoformat()}})
        presence = build_account_presence(channel, now=now)
        assert presence["state"] == expected
        assert presence["last_active_at"] == seen
    channel.last_heartbeat_at = now
    assert build_account_presence(channel, now=now)["state"] == "offline"


@pytest.mark.parametrize("raw", [None, "broken", "[]", "null", '{"webridge": []}', '{"webridge": {"last_seen_at": "bad"}}'])
def test_enabled_account_without_valid_activity_is_unknown(raw) -> None:
    channel = Channel(channel_type="wechat_webridge", is_enabled=True, status="online", runtime_info_json=raw)
    presence = build_account_presence(channel)
    assert presence["state"] == "unknown"
    assert presence["last_active_at"] is None


@pytest.mark.parametrize(
    "enabled,login_state,status,heartbeat_status,expected",
    [
        (False, "online", "online", "ok", "disabled"),
        (True, "offline", "degraded", "failed", "offline"),
        (True, "reconnecting", "degraded", "failed", "reconnecting"),
        (True, "qrcode_ready", "online", "ok", "offline"),
        (True, "qrcode_expired", "online", "ok", "offline"),
        (True, "qrcode_confirmed", "online", "ok", "offline"),
        (True, "login_init_failed", "online", "ok", "offline"),
        (True, "unknown", "online", "ok", "unknown"),
        (True, "online", "degraded", "ok", "error"),
        (True, "online", "online", "ok", "online"),
    ],
)
def test_ipad_failure_and_disabled_state_override_recent_check(enabled, login_state, status, heartbeat_status, expected) -> None:
    now = now_shanghai()
    channel = Channel(
        channel_type="wechat_ipad", is_enabled=enabled, login_state=login_state, status=status,
        last_heartbeat_at=now, runtime_info_json=json.dumps({"heartbeat_status": heartbeat_status}),
    )
    presence = build_account_presence(channel, now=now)
    assert presence["state"] == expected
    if heartbeat_status == "failed":
        assert presence["last_active_at"] is None


def test_failed_ipad_sync_does_not_refresh_online_evidence() -> None:
    now = now_shanghai()
    channel = Channel(
        channel_type="wechat_ipad", is_enabled=True, login_state="online", status="online",
        last_heartbeat_at=now - timedelta(hours=1), last_message_sync_at=now,
        runtime_info_json=json.dumps({"reconnect_status": "required"}),
    )
    assert build_account_presence(channel, now=now)["state"] == "offline"


def test_latest_successful_activity_handles_naive_dates_and_ignores_future_dates() -> None:
    now = now_shanghai()
    latest = now - timedelta(minutes=1)
    channel = Channel(
        channel_type="telegram", is_enabled=True, status="online",
        last_heartbeat_at=(now - timedelta(hours=1)).replace(tzinfo=None),
        last_message_sync_at=latest.replace(tzinfo=None), last_login_at=now + timedelta(days=1),
    )
    presence = build_account_presence(channel, now=now)
    assert presence["state"] == "online"
    assert presence["last_active_at"] == latest
    assert presence["activity_text"] == "1 分钟前活跃"
    channel.runtime_info_json = json.dumps({"telegram_polling": {"last_poll_ok": False, "last_poll_at": now.isoformat()}})
    assert build_account_presence(channel, now=now)["state"] == "error"


def test_workwechat_requires_recent_node_and_online_account() -> None:
    now = now_shanghai()
    channel = Channel(channel_type="workwechat", is_enabled=True, status="online")
    channel.provider_node = ProviderNode(status="active", last_heartbeat_at=now)
    channel.provider_account = ProviderNodeAccount(status="online", last_seen_at=now)
    assert build_account_presence(channel, now=now)["state"] == "online"
    channel.provider_node.last_heartbeat_at = now - timedelta(minutes=6)
    assert build_account_presence(channel, now=now)["state"] == "offline"
    channel.provider_node.last_heartbeat_at = now
    channel.provider_account.status = "offline"
    assert build_account_presence(channel, now=now)["state"] == "offline"
    channel.provider_account.status = "unknown"
    assert build_account_presence(channel, now=now)["state"] == "unknown"
