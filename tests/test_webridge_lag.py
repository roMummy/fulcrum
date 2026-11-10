"""微桥消息上报延迟：单条事件延迟计算与分位数。"""
import json

from app.services.wechat_webridge.dashboard import _event_lag_seconds, _percentile


def _payload(created, captured_ms, event_name="message"):
    return json.dumps({"event_name": event_name, "captured_at_ms": captured_ms, "body": {"create_time": created}})


def test_lag_is_captured_minus_created():
    assert _event_lag_seconds(_payload(1000, 1_083_000)) == 83


def test_lag_ignores_non_message_missing_and_negative():
    assert _event_lag_seconds(_payload(1000, 1_083_000, event_name="group_member_join")) is None
    assert _event_lag_seconds(json.dumps({"body": {}})) is None
    assert _event_lag_seconds(_payload(1000, 999_000)) is None  # 时钟偏差为负不计入
    assert _event_lag_seconds("not json") is None


def test_percentile_p90():
    values = list(range(1, 11))  # 1..10
    assert _percentile(values, 0.9) == 9
    assert _percentile([5], 0.9) == 5


def test_build_lag_reads_recent_raw_events():
    """走真实查询路径：只统计本渠道、微桥来源、24 小时内的消息事件。"""
    import os, tempfile
    os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="lag-logs-"))
    from datetime import timedelta
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.tenant_context import tenant_scope
    from app.core.tenant_db import tenant_session
    from app.core.time import now_shanghai
    from app.models.entities import RawWebhookEvent
    from app.services.wechat_webridge.dashboard import build_webridge_lag

    engine = create_engine("sqlite://")
    RawWebhookEvent.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = now_shanghai()
    with tenant_scope(1):
        with tenant_session(factory) as db:
            def add(channel_id, source, lag, age_minutes):
                ts = (now - timedelta(minutes=age_minutes)).replace(microsecond=0)  # 取整秒，避免截断造成 1 秒误差
                db.add(RawWebhookEvent(
                    tenant_id=1, channel_id=channel_id, source=source, status="received",
                    payload_json=_payload(int(ts.timestamp()) - lag, int(ts.timestamp() * 1000)),
                    created_at=ts,
                ))
            add(2, "wechat_webridge", 100, 5)
            add(2, "wechat_webridge", 2, 10)
            add(3, "wechat_webridge", 999, 5)   # 其他渠道不计入
            add(2, "other", 999, 5)             # 其他来源不计入
            add(2, "wechat_webridge", 500, 60 * 30)  # 超出 24 小时不计入
            db.commit()
            result = build_webridge_lag(db, SimpleNamespace(id=2))
    assert result["recent_count"] == 2
    assert result["recent_max"] == 100
    assert result["recent_p90"] == 100
    assert result["warn"] is True
    assert result["chart"]["items"]
