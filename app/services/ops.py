from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import (
    AlertRecord,
    AuditLog,
    Bot,
    BotPlugin,
    BotTagLink,
    Channel,
    Conversation,
    HealthCheckRecord,
    Message,
    RawWebhookEvent,
)


KNOWN_ENVIRONMENTS = {"prod", "test", "staging", "dev"}


def normalize_environment(value: str | None) -> str:
    normalized = (value or "prod").strip().lower() or "prod"
    return normalized if normalized in KNOWN_ENVIRONMENTS else "prod"


def normalize_environment_filter(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in KNOWN_ENVIRONMENTS else ""


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


def clone_bot(
    db: Session,
    source_bot: Bot,
    *,
    name: str,
    slug: str,
    environment: str,
    copy_channels: bool,
    copy_plugins: bool,
) -> Bot:
    target_environment = normalize_environment(environment)
    keep_runtime_enabled = target_environment == source_bot.environment

    clone = Bot(
        name=name.strip(),
        slug=slug.strip(),
        description=source_bot.description,
        status="running" if source_bot.is_enabled and keep_runtime_enabled else "stopped",
        command_prefix=source_bot.command_prefix,
        group_id=source_bot.group_id,
        config_template_id=source_bot.config_template_id,
        environment=target_environment,
        is_enabled=source_bot.is_enabled if keep_runtime_enabled else False,
    )
    db.add(clone)
    db.flush()

    for link in source_bot.tag_links:
        db.add(BotTagLink(bot_id=clone.id, tag_id=link.tag_id))

    if copy_channels:
        for channel in source_bot.channels:
            db.add(
                Channel(
                    bot_id=clone.id,
                    name=f"{channel.name} ({target_environment})",
                    channel_type=channel.channel_type,
                    api_base=channel.api_base,
                    bot_token_encrypted=channel.bot_token_encrypted,
                    bot_token_masked=channel.bot_token_masked,
                    webhook_path=channel.webhook_path,
                    dm_policy=channel.dm_policy,
                    allow_from=channel.allow_from,
                    adapter_config_json=channel.adapter_config_json,
                    status="online" if channel.is_enabled and keep_runtime_enabled else "offline",
                    is_enabled=channel.is_enabled if keep_runtime_enabled else False,
                )
            )

    if copy_plugins:
        for binding in source_bot.plugin_bindings:
            db.add(
                BotPlugin(
                    bot_id=clone.id,
                    plugin_id=binding.plugin_id,
                    status=binding.status if keep_runtime_enabled else "disabled",
                    runtime_status="idle" if keep_runtime_enabled else "disabled",
                    config_json=binding.config_json,
                )
            )

    db.commit()
    db.refresh(clone)
    return clone


def build_environment_summary(db: Session) -> list[dict[str, int | str]]:
    rows = []
    for environment in sorted(KNOWN_ENVIRONMENTS):
        rows.append(
            {
                "environment": environment,
                "bots": db.scalar(select(func.count(Bot.id)).where(Bot.environment == environment)) or 0,
                "enabled_bots": db.scalar(
                    select(func.count(Bot.id)).where(Bot.environment == environment, Bot.is_enabled.is_(True))
                )
                or 0,
                "channels": db.scalar(
                    select(func.count(Channel.id)).join(Bot, Channel.bot_id == Bot.id).where(Bot.environment == environment)
                )
                or 0,
            }
        )
    return rows


def resolve_related_environment(
    db: Session,
    related_type: str | None,
    related_id: int | None,
    cache: dict[tuple[str, int], str | None] | None = None,
) -> str | None:
    if not related_type or not related_id:
        return None

    key = (related_type, related_id)
    if cache is not None and key in cache:
        return cache[key]

    environment: str | None = None
    if related_type == "bot":
        bot = db.get(Bot, related_id)
        environment = bot.environment if bot else None
    elif related_type == "channel":
        channel = db.get(Channel, related_id)
        environment = channel.bot.environment if channel and channel.bot else None
    elif related_type == "bot_plugin":
        binding = db.get(BotPlugin, related_id)
        environment = binding.bot.environment if binding and binding.bot else None
    elif related_type == "conversation":
        conversation = db.get(Conversation, related_id)
        environment = conversation.bot.environment if conversation and conversation.bot else None
    elif related_type == "message":
        message = db.get(Message, related_id)
        environment = message.conversation.bot.environment if message and message.conversation and message.conversation.bot else None
    elif related_type == "raw_webhook_event":
        event = db.get(RawWebhookEvent, related_id)
        environment = event.channel.bot.environment if event and event.channel and event.channel.bot else None

    if cache is not None:
        cache[key] = environment
    return environment


def build_monitoring_snapshot(db: Session, environment: str = "") -> dict[str, object]:
    since = now_shanghai() - timedelta(days=1)
    normalized_environment = normalize_environment_filter(environment)
    environment_cache: dict[tuple[str, int], str | None] = {}

    def matches_env(related_type: str | None, related_id: int | None) -> bool:
        if not normalized_environment:
            return True
        return resolve_related_environment(db, related_type, related_id, cache=environment_cache) == normalized_environment

    recent_health_checks = [
        item
        for item in db.scalars(select(HealthCheckRecord).order_by(HealthCheckRecord.id.desc()).limit(100)).all()
        if matches_env(item.target_type, item.target_id)
    ][:20]
    open_alerts = [
        item
        for item in db.scalars(select(AlertRecord).where(AlertRecord.status == "open").order_by(AlertRecord.id.desc()).limit(100)).all()
        if matches_env(item.related_type, item.related_id)
    ][:20]
    recent_audits = [
        item
        for item in db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(150)).all()
        if matches_env(item.target_type, item.target_id)
    ][:30]

    all_health_checks = db.scalars(select(HealthCheckRecord).order_by(HealthCheckRecord.id.desc()).limit(500)).all()
    all_open_alerts = db.scalars(select(AlertRecord).where(AlertRecord.status == "open").order_by(AlertRecord.id.desc()).limit(500)).all()
    all_audits = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(500)).all()

    health_check_count = sum(1 for item in all_health_checks if matches_env(item.target_type, item.target_id))
    open_alert_count = sum(1 for item in all_open_alerts if matches_env(item.related_type, item.related_id))
    audit_log_count = sum(1 for item in all_audits if matches_env(item.target_type, item.target_id))

    message_stmt = select(func.count(Message.id)).join(Conversation, Message.conversation_id == Conversation.id)
    if normalized_environment:
        message_stmt = message_stmt.join(Bot, Conversation.bot_id == Bot.id).where(Bot.environment == normalized_environment)
    message_stmt = message_stmt.where(Message.created_at >= since)
    messages_24h = db.scalar(message_stmt) or 0

    webhook_failures_24h = sum(
        1
        for item in all_open_alerts
        if item.source == "webhook"
        and _normalize_datetime(item.created_at) >= since
        and matches_env(item.related_type, item.related_id)
    )

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
        "environment": normalized_environment,
    }


def _normalize_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=now_shanghai().tzinfo)
    return ensure_shanghai_datetime(value)


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
                "/home/liuwei/abot/wechat_ipad/server/swagger/swagger.yml",
                "/home/liuwei/abot/wechat_ipad/server/swagger/swagger.json",
            ],
            "notes": [
                "当前仅提供统一出站接口骨架与 channel 适配占位。",
                "wechat_ipad 的真实字段映射、鉴权头、响应结构需与 170 abot swagger 二次对齐。",
            ],
        },
    }
