from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import (
    AlertRecord,
    AuditLog,
    Channel,
    Conversation,
    HealthCheckRecord,
    Message,
)


def record_audit(
    db: Session,
    action: str,
    target_type: str,
    target_id: int | None = None,
    detail: str | None = None,
    actor: str = "admin-console",
) -> AuditLog:
    item = AuditLog(actor=actor, action=action, target_type=target_type, target_id=target_id, detail=detail)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def record_health_check(
    db: Session,
    scope: str,
    target_type: str,
    status: str,
    summary: str,
    target_id: int | None = None,
    details: dict | None = None,
) -> HealthCheckRecord:
    item = HealthCheckRecord(
        scope=scope,
        target_type=target_type,
        target_id=target_id,
        status=status,
        summary=summary,
        details_json=json.dumps(details, ensure_ascii=False) if details else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def create_alert(
    db: Session,
    source: str,
    title: str,
    message: str,
    level: str = "warning",
    related_type: str | None = None,
    related_id: int | None = None,
    status: str = "open",
) -> AlertRecord:
    item = AlertRecord(
        source=source,
        title=title,
        message=message,
        level=level,
        related_type=related_type,
        related_id=related_id,
        status=status,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def build_monitoring_snapshot(db: Session) -> dict[str, object]:
    since = now_shanghai() - timedelta(days=1)

    # 环境筛选已从产品中删除，监控直接展示当前租户的完整事件流，避免同一批
    # 告警因为旧环境字段缺失而被静默过滤。
    recent_health_checks = db.scalars(select(HealthCheckRecord).order_by(HealthCheckRecord.id.desc()).limit(20)).all()
    open_alerts = db.scalars(
        select(AlertRecord).where(AlertRecord.status == "open").order_by(AlertRecord.id.desc()).limit(20)
    ).all()
    recent_audits = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(30)).all()

    health_check_count = db.scalar(select(func.count(HealthCheckRecord.id))) or 0
    open_alert_count = db.scalar(select(func.count(AlertRecord.id)).where(AlertRecord.status == "open")) or 0
    audit_log_count = db.scalar(select(func.count(AuditLog.id))) or 0
    messages_24h = db.scalar(select(func.count(Message.id)).where(Message.created_at >= since)) or 0

    webhook_failures_24h = db.scalar(
        select(func.count(AlertRecord.id)).where(
            AlertRecord.status == "open",
            AlertRecord.source == "webhook",
            AlertRecord.created_at >= since,
        )
    ) or 0

    stats = {
        "health_checks": health_check_count,
        "open_alerts": open_alert_count,
        "audit_logs": audit_log_count,
        "messages_24h": messages_24h,
        "webhook_failures_24h": webhook_failures_24h,
    }
    return {
        "recent_health_checks": recent_health_checks,
        "open_alerts": open_alerts,
        "recent_audits": recent_audits,
        "stats": stats,
    }
def build_outbound_stub(channel: Channel, conversation: Conversation, content: str) -> dict[str, object]:
    return {
        "channel_id": channel.id,
        "channel_type": channel.channel_type,
        "api_base": channel.api_base,
        "conversation_id": conversation.id,
        "conversation_external_id": conversation.external_id,
        "content": content,
        "transport_status": "queued",
        "swagger_alignment": {
            "status": "pending-170-abot-swagger",
            "reference_files": [
                "wechat_ipad/server/swagger/swagger.yml",
                "wechat_ipad/server/swagger/swagger.json",
            ],
            "notes": [
                "当前仅提供统一出站接口骨架与 channel 适配占位。",
                "wechat_ipad 的真实字段映射、鉴权头、响应结构需与 170 abot swagger 二次对齐。",
            ],
        },
    }
