from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.text_column import dump_json_for_text_column
from app.core.time import ensure_shanghai_datetime, hour_bucket_shanghai, now_shanghai
from app.models.entities import (
    AlertRecord,
    Bot,
    BotPlugin,
    Channel,
    Conversation,
    Message,
    MessageActivityHourly,
    Plugin,
    PluginActivityHourly,
    PluginExecutionLog,
    TaskJob,
)


def _utcnow() -> datetime:
    return now_shanghai()


def _hour_bucket(value: datetime | None = None) -> datetime:
    return hour_bucket_shanghai(value or _utcnow())


def record_message_activity(
    db: Session,
    *,
    channel: Channel,
    conversation: Conversation,
    message: Message,
) -> None:
    bucket = _hour_bucket(getattr(message, "created_at", None))
    row = db.scalar(
        select(MessageActivityHourly).where(
            MessageActivityHourly.bucket_start == bucket,
            MessageActivityHourly.bot_id == channel.bot_id,
            MessageActivityHourly.channel_id == channel.id,
            MessageActivityHourly.channel_type == channel.channel_type,
            MessageActivityHourly.chat_type == conversation.chat_type,
            MessageActivityHourly.direction == message.direction,
        )
    )
    if row is None:
        row = MessageActivityHourly(
            bucket_start=bucket,
            bot_id=channel.bot_id,
            channel_id=channel.id,
            channel_type=channel.channel_type,
            chat_type=conversation.chat_type,
            direction=message.direction,
            message_count=0,
        )
        db.add(row)
        db.flush()
    row.message_count += 1


def record_plugin_dispatch_analytics(
    db: Session,
    *,
    channel: Channel,
    dispatch_report: Any,
    raw_event_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
) -> None:
    context = getattr(dispatch_report, "static_plan", None).context if getattr(dispatch_report, "static_plan", None) else None
    decisions = {
        str(item.candidate.plugin_code): item
        for item in (getattr(dispatch_report, "decisions", ()) or ())
    }
    bucket = _hour_bucket()
    for execution in getattr(dispatch_report, "executions", ()) or ():
        candidate = getattr(execution, "candidate", None)
        if candidate is None:
            continue
        decision = decisions.get(str(candidate.plugin_code))
        debug = dict(getattr(execution, "debug", {}) or {})
        duration_ms = int(debug.get("duration_ms") or 0)
        outbound_count = len(debug.get("auto_outbound") or []) if isinstance(debug.get("auto_outbound"), list) else 0
        status = str(getattr(execution, "status", "") or "skipped").strip().lower() or "skipped"
        message = str(getattr(execution, "message", "") or "").strip()
        matched = bool(getattr(decision, "matched", False))
        attempted = bool(getattr(execution, "attempted", False))
        ok = bool(getattr(execution, "ok", False))
        db.add(
            PluginExecutionLog(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                conversation_id=conversation_id,
                message_id=message_id,
                raw_event_id=raw_event_id,
                plugin_id=getattr(candidate, "plugin_id", None),
                bot_plugin_id=getattr(candidate, "binding_id", None),
                plugin_code=str(getattr(candidate, "plugin_code", "") or ""),
                channel_type=channel.channel_type,
                chat_type=getattr(context, "chat_type", None),
                message_type=getattr(context, "message_type", None),
                sender_id=getattr(context, "sender_id", None),
                conversation_external_id=getattr(context, "conversation_external_id", None),
                matched=matched,
                attempted=attempted,
                ok=ok,
                status=status,
                consume_policy=str(getattr(candidate, "consume_policy", "") or "") or None,
                duration_ms=duration_ms,
                outbound_count=outbound_count,
                trigger_source="inbound_dispatch",
                error_message=message if status in {"error", "timeout", "outbound_failed"} else None,
                summary=message or None,
                details_json=dump_json_for_text_column(
                    {
                        "decision_debug": dict(getattr(decision, "debug", {}) or {}) if decision else {},
                        "execution_debug": debug,
                    },
                    sort_keys=True,
                    drop_keys=("runtime_payload",),
                ),
            )
        )
        hourly = db.scalar(
            select(PluginActivityHourly).where(
                PluginActivityHourly.bucket_start == bucket,
                PluginActivityHourly.bot_id == channel.bot_id,
                PluginActivityHourly.channel_id == channel.id,
                PluginActivityHourly.plugin_id == getattr(candidate, "plugin_id", None),
                PluginActivityHourly.bot_plugin_id == getattr(candidate, "binding_id", None),
                PluginActivityHourly.plugin_code == str(getattr(candidate, "plugin_code", "") or ""),
            )
        )
        if hourly is None:
            hourly = PluginActivityHourly(
                bucket_start=bucket,
                bot_id=channel.bot_id,
                channel_id=channel.id,
                plugin_id=getattr(candidate, "plugin_id", None),
                bot_plugin_id=getattr(candidate, "binding_id", None),
                plugin_code=str(getattr(candidate, "plugin_code", "") or ""),
            )
            db.add(hourly)
            db.flush()
        if matched:
            hourly.matched_count += 1
        if attempted:
            hourly.trigger_count += 1
            hourly.attempted_count += 1
        if ok:
            hourly.success_count += 1
        if status in {"error", "outbound_failed"}:
            hourly.failed_count += 1
        if status == "skipped":
            hourly.skipped_count += 1
        if status == "blocked":
            hourly.blocked_count += 1
        if status == "timeout":
            hourly.timeout_count += 1
        hourly.outbound_count += outbound_count
        hourly.total_duration_ms += max(duration_ms, 0)


def upsert_alert(
    db: Session,
    *,
    rule_key: str,
    source: str,
    title: str,
    message: str,
    level: str = "warning",
    related_type: str | None = None,
    related_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AlertRecord:
    item = db.scalar(select(AlertRecord).where(AlertRecord.rule_key == rule_key, AlertRecord.status == "open"))
    details_json = json.dumps(details, ensure_ascii=False, sort_keys=True) if details else None
    if item is None:
        item = AlertRecord(
            source=source,
            title=title,
            message=message,
            level=level,
            related_type=related_type,
            related_id=related_id,
            rule_key=rule_key,
            status="open",
            details_json=details_json,
            resolved_at=None,
        )
        db.add(item)
        db.flush()
        return item
    item.source = source
    item.title = title
    item.message = message
    item.level = level
    item.related_type = related_type
    item.related_id = related_id
    item.details_json = details_json
    item.resolved_at = None
    return item


def resolve_missing_alerts(db: Session, *, source: str, active_rule_keys: set[str]) -> int:
    rows = db.scalars(select(AlertRecord).where(AlertRecord.source == source, AlertRecord.status == "open")).all()
    closed = 0
    for item in rows:
        key = str(item.rule_key or "").strip()
        if key and key in active_rule_keys:
            continue
        item.status = "resolved"
        item.resolved_at = now_shanghai()
        closed += 1
    return closed


def evaluate_operational_alerts(db: Session) -> dict[str, int]:
    now = _utcnow()
    active_rule_keys: set[str] = set()

    abnormal_channels = db.scalars(
        select(Channel)
        .join(Bot, Channel.bot_id == Bot.id)
        .where(
            Channel.is_enabled.is_(True),
            Channel.deleted_at.is_(None),
            Bot.deleted_at.is_(None),
            Channel.status.in_(("degraded", "offline", "failed")),
        )
    ).all()
    for channel in abnormal_channels:
        rule_key = f"channel_abnormal:{channel.id}"
        active_rule_keys.add(rule_key)
        upsert_alert(
            db,
            rule_key=rule_key,
            source="ops",
            title=f"渠道异常 #{channel.id}",
            message=f"渠道 {channel.name} 当前状态为 {channel.status}。",
            level="critical",
            related_type="channel",
            related_id=channel.id,
            details={"channel_type": channel.channel_type, "status": channel.status},
        )

    heartbeat_cutoff = now - timedelta(minutes=10)
    stale_wechat_channels = db.scalars(
        select(Channel).where(
            Channel.channel_type == "wechat_ipad",
            Channel.is_enabled.is_(True),
            Channel.deleted_at.is_(None),
            Channel.login_state == "online",
            Channel.last_heartbeat_at.is_not(None),
            Channel.last_heartbeat_at < heartbeat_cutoff,
        )
    ).all()
    for channel in stale_wechat_channels:
        rule_key = f"wechat_heartbeat_stale:{channel.id}"
        active_rule_keys.add(rule_key)
        upsert_alert(
            db,
            rule_key=rule_key,
            source="ops",
            title=f"Wechat iPad 心跳超时 #{channel.id}",
            message=f"渠道 {channel.name} 最近心跳时间过旧，请检查登录状态与 provider。",
            level="critical",
            related_type="channel",
            related_id=channel.id,
            details={"last_heartbeat_at": str(channel.last_heartbeat_at or "")},
        )

    # 只统计"新出现"的失败任务:最近 24 小时内,并且在这条告警上一次被标记处理(或自动恢复)之后产生的。
    # 失败任务的历史记录不会消失,如果按总数告警,标记已处理后下一轮评估立刻又会重新报警。
    failed_since = now - timedelta(hours=24)
    last_handled_at = db.scalar(
        select(func.max(AlertRecord.resolved_at)).where(AlertRecord.rule_key == "task_queue_failed", AlertRecord.status == "resolved")
    )
    if last_handled_at is not None and ensure_shanghai_datetime(last_handled_at) > ensure_shanghai_datetime(failed_since):
        failed_since = ensure_shanghai_datetime(last_handled_at)
    failed_jobs = db.scalar(select(func.count(TaskJob.id)).where(TaskJob.status == "failed", TaskJob.updated_at > failed_since)) or 0
    if failed_jobs > 0:
        active_rule_keys.add("task_queue_failed")
        upsert_alert(
            db,
            rule_key="task_queue_failed",
            source="ops",
            title="任务队列存在失败任务",
            message=f"最近有 {failed_jobs} 个新的失败任务，请检查任务队列与渠道发送链路；标记已处理后，只有再出现新的失败才会重新告警。",
            level="warning",
            related_type="task_job",
            related_id=None,
            details={"failed_jobs": failed_jobs},
        )

    recent_30m = now - timedelta(minutes=30)
    previous_24h = now - timedelta(hours=24)
    inbound_recent = db.scalar(select(func.count(Message.id)).where(Message.direction == "in", Message.created_at >= recent_30m)) or 0
    inbound_previous = (
        db.scalar(
            select(func.count(Message.id)).where(
                Message.direction == "in",
                Message.created_at >= previous_24h,
                Message.created_at < recent_30m,
            )
        )
        or 0
    )
    if inbound_recent == 0 and inbound_previous >= 20:
        active_rule_keys.add("inbound_stall_global")
        upsert_alert(
            db,
            rule_key="inbound_stall_global",
            source="ops",
            title="全局入站疑似停滞",
            message="最近 30 分钟没有新的入站消息，但过去 24 小时内系统存在稳定入站。",
            level="critical",
            related_type="message",
            related_id=None,
            details={"inbound_recent_30m": inbound_recent, "inbound_previous_24h": inbound_previous},
        )

    bot_cutoff = now - timedelta(hours=1)
    bot_history_cutoff = now - timedelta(days=7)
    enabled_bots = db.scalars(select(Bot).where(Bot.is_enabled.is_(True), Bot.deleted_at.is_(None))).all()
    for bot in enabled_bots:
        recent_count = (
            db.scalar(
                select(func.count(Message.id))
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.bot_id == bot.id, Message.direction == "in", Message.created_at >= bot_cutoff)
            )
            or 0
        )
        history_count = (
            db.scalar(
                select(func.count(Message.id))
                .join(Conversation, Message.conversation_id == Conversation.id)
                .where(Conversation.bot_id == bot.id, Message.direction == "in", Message.created_at >= bot_history_cutoff)
            )
            or 0
        )
        if recent_count == 0 and history_count >= 20:
            rule_key = f"bot_inbound_silent:{bot.id}"
            active_rule_keys.add(rule_key)
            upsert_alert(
                db,
                rule_key=rule_key,
                source="ops",
                title=f"机器人入站沉默 #{bot.id}",
                message=f"机器人 {bot.name} 最近 1 小时没有新的入站消息，请检查渠道与触发情况。",
                level="warning",
                related_type="bot",
                related_id=bot.id,
                details={"recent_1h": recent_count, "history_7d": history_count},
            )

    plugin_failure_cutoff = now - timedelta(hours=1)
    failing_plugins = db.execute(
        select(
            PluginExecutionLog.bot_plugin_id,
            PluginExecutionLog.plugin_id,
            PluginExecutionLog.plugin_code,
            func.count(PluginExecutionLog.id).label("attempts"),
            func.sum(case((PluginExecutionLog.status.in_(("error", "timeout", "outbound_failed")), 1), else_=0)).label("failures"),
        )
        .where(
            PluginExecutionLog.created_at >= plugin_failure_cutoff,
            PluginExecutionLog.attempted.is_(True),
        )
        .group_by(
            PluginExecutionLog.bot_plugin_id,
            PluginExecutionLog.plugin_id,
            PluginExecutionLog.plugin_code,
        )
    ).all()
    for row in failing_plugins:
        attempts = int(row.attempts or 0)
        failures = int(row.failures or 0)
        if attempts < 5 or failures <= 0 or failures / max(attempts, 1) < 0.5:
            continue
        binding_id = int(row.bot_plugin_id or 0)
        rule_key = f"plugin_failure_burst:{binding_id or row.plugin_code}"
        active_rule_keys.add(rule_key)
        upsert_alert(
            db,
            rule_key=rule_key,
            source="plugin",
            title=f"插件失败率过高 {row.plugin_code}",
            message=f"插件 {row.plugin_code} 最近 1 小时执行 {attempts} 次，失败 {failures} 次。",
            level="warning",
            related_type="bot_plugin" if binding_id else "plugin",
            related_id=binding_id or int(row.plugin_id or 0) or None,
            details={"attempts": attempts, "failures": failures},
        )

    resolved_ops = resolve_missing_alerts(db, source="ops", active_rule_keys=active_rule_keys)
    resolved_plugin = resolve_missing_alerts(db, source="plugin", active_rule_keys=active_rule_keys)
    db.commit()
    return {
        "active": len(active_rule_keys),
        "resolved": resolved_ops + resolved_plugin,
    }
