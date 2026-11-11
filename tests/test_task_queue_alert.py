"""运维告警「任务队列存在失败任务」:只对新出现的失败告警,标记已处理后不会因为历史失败又冒出来。

线上事故:历史失败任务(微桥出站超时、缺模块的插件分发)永远留在表里,原规则按总数告警,
用户点"已处理"后,下一轮评估又新建了一条同样的告警。
"""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="task-queue-alert-logs-"))

from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import AlertRecord, TaskJob
from app.services.analytics import evaluate_operational_alerts
from tests.test_ai_chat import _session

RULE = "task_queue_failed"


def _failed_job(db, *, hours_ago: float, dedupe: str) -> TaskJob:
    moment = now_shanghai() - timedelta(hours=hours_ago)
    job = TaskJob(job_type="wechat_webridge_outbound", status="failed", payload_json="{}", dedupe_key=dedupe, created_at=moment, updated_at=moment)
    db.add(job)
    db.flush()
    # updated_at 带 onupdate,flush 后再显式定回目标时间,模拟"那个时间点失败"
    job.updated_at = moment
    db.flush()
    return job


def _alerts(db, status: str) -> list[AlertRecord]:
    return list(db.scalars(select(AlertRecord).where(AlertRecord.rule_key == RULE, AlertRecord.status == status)).all())


def test_old_failed_jobs_do_not_raise_the_alert() -> None:
    db = _session()
    _failed_job(db, hours_ago=72, dedupe="old-1")
    _failed_job(db, hours_ago=30, dedupe="old-2")
    evaluate_operational_alerts(db)
    assert _alerts(db, "open") == []


def test_recent_failed_jobs_raise_one_alert_with_the_new_count() -> None:
    db = _session()
    _failed_job(db, hours_ago=72, dedupe="old")
    _failed_job(db, hours_ago=2, dedupe="new-1")
    _failed_job(db, hours_ago=1, dedupe="new-2")
    evaluate_operational_alerts(db)
    evaluate_operational_alerts(db)  # 重复评估不会产生第二条
    (alert,) = _alerts(db, "open")
    assert "2 个新的失败任务" in alert.message


def test_marking_it_handled_keeps_it_closed_until_a_new_failure_happens() -> None:
    db = _session()
    _failed_job(db, hours_ago=3, dedupe="before")
    evaluate_operational_alerts(db)
    (alert,) = _alerts(db, "open")
    # 用户点"已处理"
    alert.status, alert.resolved_at = "resolved", now_shanghai()
    db.flush()
    evaluate_operational_alerts(db)
    assert _alerts(db, "open") == []  # 旧失败仍在表里,但不再重新告警
    # 处理之后又出现新的失败:才重新告警,且只统计这一个
    _failed_job(db, hours_ago=-0.01, dedupe="after")
    evaluate_operational_alerts(db)
    (again,) = _alerts(db, "open")
    assert "1 个新的失败任务" in again.message
