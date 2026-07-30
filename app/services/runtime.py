from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.core.db import engine
from app.core.settings import settings
from app.core.time import now_shanghai
from app.models.entities import (
    AlertRecord,
    AuditLog,
    Bot,
    BotPlugin,
    Chatroom,
    ChatroomMember,
    Channel,
    Conversation,
    CustomerOperationRequest,
    GroupPluginScope,
    HealthCheckRecord,
    Message,
    MessageAttachment,
    MessageActivityHourly,
    PointsAccount,
    PointsPrisonRecord,
    PointsTransaction,
    PluginActivityHourly,
    PluginExecutionLog,
    RawWebhookEvent,
    SignInHistory,
    SignInRecord,
    TaskJob,
    WeatherSubscription,
    WorkWechatContact,
    WorkWechatContactRelation,
)
from app.services.plugin_runtime.contracts import PluginExecutionResult
from app.services.message_state import refresh_conversation_last_message_at
from app.services.security import decrypt_secret


def ensure_runtime_schema() -> None:
    inspector = inspect(engine)
    _ensure_large_text_columns(
        (
            ("task_jobs", "payload_json"),
            ("task_jobs", "result_json"),
            ("raw_webhook_events", "payload_json"),
            ("raw_webhook_events", "dispatch_summary_json"),
            ("messages", "raw_request_json"),
            ("messages", "raw_response_json"),
            ("bot_plugins", "runtime_details_json"),
            ("plugin_execution_logs", "details_json"),
            ("health_check_records", "details_json"),
            ("alert_records", "details_json"),
        )
    )

    _ensure_column("channels", "bot_token_encrypted", "TEXT")
    _ensure_column("channels", "adapter_config_json", "TEXT")
    _ensure_column("channels", "login_state", "VARCHAR(30) DEFAULT 'unknown'")
    _ensure_column("channels", "qrcode_status", "VARCHAR(30) DEFAULT 'idle'")
    _ensure_column("channels", "last_login_at", "DATETIME")
    _ensure_column("channels", "last_heartbeat_at", "DATETIME")
    _ensure_column("channels", "last_message_sync_at", "DATETIME")
    _ensure_column("channels", "runtime_info_json", "TEXT")
    _ensure_column("messages", "external_status", "VARCHAR(30)")
    _ensure_column("messages", "raw_request_json", "TEXT")
    _ensure_column("messages", "raw_response_json", "TEXT")
    _ensure_column("bot_plugins", "runtime_status", "VARCHAR(20) DEFAULT 'idle'")
    _ensure_column("bot_plugins", "last_reload_at", "DATETIME")
    _ensure_column("bot_plugins", "last_execution_at", "DATETIME")
    _ensure_column("bot_plugins", "last_execution_status", "VARCHAR(20)")
    _ensure_column("bot_plugins", "last_execution_summary", "TEXT")
    _ensure_column("bot_plugins", "runtime_details_json", "TEXT")
    _ensure_column("conversations", "channel_id", "INTEGER")

    tables = set(inspector.get_table_names())
    if "raw_webhook_events" not in tables:
        RawWebhookEvent.__table__.create(bind=engine)
    else:
        _ensure_column("raw_webhook_events", "dispatch_summary_json", "TEXT")
    if "health_check_records" not in tables:
        HealthCheckRecord.__table__.create(bind=engine)
    if "alert_records" not in tables:
        AlertRecord.__table__.create(bind=engine)
    else:
        _ensure_column("alert_records", "rule_key", "VARCHAR(200)")
        _ensure_column("alert_records", "details_json", "TEXT")
        _ensure_column("alert_records", "resolved_at", "DATETIME")
    if "audit_logs" not in tables:
        AuditLog.__table__.create(bind=engine)
    if "plugin_execution_logs" not in tables:
        PluginExecutionLog.__table__.create(bind=engine)
    if "message_activity_hourly" not in tables:
        MessageActivityHourly.__table__.create(bind=engine)
    if "plugin_activity_hourly" not in tables:
        PluginActivityHourly.__table__.create(bind=engine)
    if "admin_users" not in tables:
        from app.models.entities import AdminUser

        AdminUser.__table__.create(bind=engine)
    else:
        _ensure_column("admin_users", "auth_version", "INTEGER NOT NULL DEFAULT 1")
        with engine.begin() as connection:
            connection.execute(text("UPDATE admin_users SET auth_version = COALESCE(auth_version, 1)"))
    if "chatrooms" not in tables:
        Chatroom.__table__.create(bind=engine)
    else:
        _ensure_column("chatrooms", "registration_source", "VARCHAR(20) DEFAULT 'passive'")
        _ensure_column("chatrooms", "management_status", "VARCHAR(30) DEFAULT 'discovered'")
        _ensure_column("chatrooms", "is_registered", "BOOLEAN DEFAULT 1")
        _ensure_column("chatrooms", "is_active", "BOOLEAN DEFAULT 0")
        _ensure_column("chatrooms", "first_seen_at", "DATETIME")
        _ensure_column("chatrooms", "last_seen_at", "DATETIME")
        _ensure_column("chatrooms", "operator_note", "TEXT")
    if "chatroom_members" not in tables:
        ChatroomMember.__table__.create(bind=engine)
    else:
        _ensure_column("chatroom_members", "last_active_at", "DATETIME")
    if "workwechat_contacts" not in tables:
        WorkWechatContact.__table__.create(bind=engine)
    else:
        _ensure_column("workwechat_contacts", "conversation_external_id", "VARCHAR(128)")
    if "workwechat_contact_relations" not in tables:
        WorkWechatContactRelation.__table__.create(bind=engine)
    if "customer_operation_requests" not in tables:
        CustomerOperationRequest.__table__.create(bind=engine)
    if "group_plugin_scopes" not in tables:
        GroupPluginScope.__table__.create(bind=engine)
    if "task_jobs" not in tables:
        TaskJob.__table__.create(bind=engine)
    if "message_attachments" not in tables:
        MessageAttachment.__table__.create(bind=engine)
    if "weather_subscriptions" not in tables:
        WeatherSubscription.__table__.create(bind=engine)
    else:
        _ensure_column("weather_subscriptions", "config_json", "TEXT")
    if "sign_in_records" not in tables:
        SignInRecord.__table__.create(bind=engine)
    if "sign_in_history" not in tables:
        SignInHistory.__table__.create(bind=engine)
    if "points_accounts" not in tables:
        PointsAccount.__table__.create(bind=engine)
    if "points_transactions" not in tables:
        PointsTransaction.__table__.create(bind=engine)
    if "points_prison_records" not in tables:
        PointsPrisonRecord.__table__.create(bind=engine)


def _ensure_column(table_name: str, column_name: str, definition: str) -> None:
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"))


def _ensure_large_text_column(table_name: str, column_name: str) -> None:
    database_url = settings.database_url.strip().lower()
    if not (database_url.startswith("mysql") or database_url.startswith("mariadb")):
        return
    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    column = columns.get(column_name)
    if not column:
        return
    type_name = str(column.get("type") or "").strip().lower()
    if "longtext" in type_name:
        return
    nullable = bool(column.get("nullable", True))
    null_clause = "NULL" if nullable else "NOT NULL"
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} LONGTEXT {null_clause}"))


def _ensure_large_text_columns(targets: tuple[tuple[str, str], ...]) -> None:
    for table_name, column_name in targets:
        _ensure_large_text_column(table_name, column_name)


def backfill_conversation_channels(db: Session) -> None:
    conversations = db.scalars(select(Conversation).where(Conversation.channel_id.is_(None))).all()
    if not conversations:
        return

    channels = db.scalars(select(Channel)).all()
    changed = False
    for conversation in conversations:
        for channel in channels:
            if channel.bot_id == conversation.bot_id and channel.channel_type == conversation.channel_type:
                conversation.channel_id = channel.id
                changed = True
                break
    if changed:
        db.commit()


def run_bot_status_check(db: Session, bot: Bot) -> tuple[str, str]:
    enabled_channels = [item for item in bot.channels if item.is_enabled]
    enabled_bindings = [item for item in bot.plugin_bindings if item.status == "enabled"]

    if not bot.is_enabled:
        bot.status = "stopped"
        db.commit()
        return bot.status, "机器人已停用，跳过运行检查。"

    if not enabled_channels:
        bot.status = "degraded"
        db.commit()
        return bot.status, "缺少启用中的渠道。"

    if not enabled_bindings:
        bot.status = "degraded"
        db.commit()
        return bot.status, "缺少启用中的插件绑定。"

    bot.status = "running"
    db.commit()
    return bot.status, f"检查通过：{len(enabled_channels)} 个渠道、{len(enabled_bindings)} 个插件绑定可用。"


def run_channel_health_check(db: Session, channel: Channel) -> tuple[str, str]:
    if not channel.is_enabled:
        channel.status = "offline"
        db.commit()
        return channel.status, "渠道已停用。"

    issues: list[str] = []
    # 企业微信云端 WS/拉取模式由本地 provider 主动注册、主动领取任务，
    # 主服务不再需要反向访问企业内网 provider，因此不要求 API Base 和 Webhook Path。
    from app.services.workwechat_cloud_poll import is_cloud_poll_enabled

    if not is_cloud_poll_enabled(channel):
        parsed = urlparse(channel.api_base or "")
        if not parsed.scheme or not parsed.netloc:
            issues.append("API Base 缺失或格式非法")
        if not channel.webhook_path:
            issues.append("Webhook Path 缺失")
    if not decrypt_secret(channel.bot_token_encrypted):
        issues.append("Bot Token 未配置")

    channel.status = "degraded" if issues else "online"
    db.commit()
    if issues:
        return channel.status, "；".join(issues)
    if is_cloud_poll_enabled(channel):
        return channel.status, "检查通过，企微云端 WS/拉取模式不需要 API Base。"
    return channel.status, "检查通过，核心接入配置齐全。"


def reload_binding(db: Session, binding: BotPlugin) -> tuple[str, str]:
    if binding.status != "enabled":
        binding.runtime_status = "disabled"
        db.commit()
        return binding.runtime_status, "插件绑定当前为 disabled，未执行重载。"

    from app.services.plugin_runtime import execute_binding_action

    result = execute_binding_action(binding, "reload")
    if result is None:
        binding.runtime_status = "running"
        binding.last_reload_at = now_shanghai()
        binding.last_execution_at = binding.last_reload_at
        binding.last_execution_status = "running"
        binding.last_execution_summary = "插件已完成模拟重载。"
        binding.runtime_details_json = json.dumps({"action": "reload", "mode": "legacy-fallback"}, ensure_ascii=False, sort_keys=True)
        db.commit()
        return binding.runtime_status, "插件已完成模拟重载。"

    persist_binding_execution(db, binding, result, action="reload")
    binding.last_reload_at = binding.last_execution_at
    return binding.runtime_status, result.message


def compute_binding_runtime(binding: BotPlugin) -> str:
    if binding.status != "enabled":
        return "disabled"
    if not binding.bot.is_enabled:
        return "waiting-bot"
    enabled_channels = any(channel.is_enabled for channel in binding.bot.channels)
    if not enabled_channels:
        return "waiting-channel"
    runtime_status = (binding.runtime_status or "").strip()
    if runtime_status and runtime_status not in {"idle", "disabled"}:
        return runtime_status
    if binding.last_execution_status:
        return binding.last_execution_status
    return "running"


def persist_binding_execution(
    db: Session,
    binding: BotPlugin,
    result: PluginExecutionResult,
    *,
    action: str,
) -> None:
    binding.runtime_status = result.status
    binding.last_execution_at = now_shanghai()
    binding.last_execution_status = result.status
    binding.last_execution_summary = result.message
    binding.runtime_details_json = json.dumps(
        {
            "action": action,
            "ok": result.ok,
            "status": result.status,
            "message": result.message,
            "output": dict(result.output),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    db.commit()


def build_binding_runtime_view(binding: BotPlugin) -> dict[str, object]:
    details = _parse_json(getattr(binding, "runtime_details_json", None))
    return {
        "display_status": compute_binding_runtime(binding),
        "last_execution_at": getattr(binding, "last_execution_at", None),
        "last_execution_status": getattr(binding, "last_execution_status", None),
        "last_execution_summary": getattr(binding, "last_execution_summary", None),
        "runtime_details": details,
        "last_action": details.get("action"),
    }


def build_binding_dispatch_view(db: Session, binding: BotPlugin) -> dict[str, object]:
    plugin = binding.plugin
    if plugin is None:
        return {}

    channel_ids = [channel.id for channel in binding.bot.channels]
    if not channel_ids:
        return {}

    latest_events = db.scalars(
        select(RawWebhookEvent)
        .where(RawWebhookEvent.channel_id.in_(channel_ids), RawWebhookEvent.dispatch_summary_json.is_not(None))
        .order_by(RawWebhookEvent.id.desc())
        .limit(20)
    ).all()

    for raw_event in latest_events:
        summary = _parse_json(getattr(raw_event, "dispatch_summary_json", None))
        if not summary:
            continue
        view = _extract_plugin_dispatch_view(summary, plugin.code)
        if not view:
            continue
        return {
            **view,
            "raw_event_id": raw_event.id,
            "created_at": getattr(raw_event, "created_at", None),
            "event_key": getattr(raw_event, "event_key", None),
        }
    return {}


def append_manual_message(
    db: Session,
    conversation: Conversation,
    content: str,
    sender_name: str = "Admin Console",
) -> Message:
    message = Message(
        conversation_id=conversation.id,
        direction="out",
        sender_id="admin-console",
        sender_name=sender_name,
        content=content.strip(),
        message_type="text",
        status="queued",
    )
    db.add(message)
    db.flush()
    refresh_conversation_last_message_at(db, conversation.id)
    db.commit()
    db.refresh(message)
    return message


def _parse_json(raw: str | None) -> dict[str, object]:
    normalized = (raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_plugin_dispatch_view(summary: dict[str, object], plugin_code: str) -> dict[str, object]:
    static_block = summary.get("static")
    static_data = static_block if isinstance(static_block, dict) else {}
    decisions = _as_dict_list(summary.get("decisions"))
    executions = _as_dict_list(summary.get("executions"))
    skipped = _as_dict_list(static_data.get("skipped"))

    decision = next((item for item in decisions if item.get("plugin_code") == plugin_code), None)
    execution = next((item for item in executions if item.get("plugin_code") == plugin_code), None)
    skipped_item = next((item for item in skipped if item.get("plugin_code") == plugin_code), None)
    stop_reason = str(summary.get("stop_reason") or "").strip() or None

    if execution:
        debug = execution.get("debug") if isinstance(execution.get("debug"), dict) else {}
        return {
            "status": str(execution.get("status") or "unknown"),
            "attempted": bool(execution.get("attempted")),
            "ok": bool(execution.get("ok")),
            "message": str(execution.get("message") or "-"),
            "execution_source": str(debug.get("execution_source") or "-"),
            "matched": bool(decision.get("matched")) if decision else bool(execution.get("attempted")),
            "decision_reason": str(decision.get("reason") or "-") if decision else "-",
            "stop_reason": stop_reason,
            "blocked_by_plugin_code": str(debug.get("blocked_by_plugin_code") or "").strip() or None,
        }

    if decision:
        return {
            "status": "matched" if decision.get("matched") else "skipped",
            "attempted": False,
            "ok": bool(decision.get("matched")),
            "message": str(decision.get("reason") or "-"),
            "execution_source": "-",
            "matched": bool(decision.get("matched")),
            "decision_reason": str(decision.get("reason") or "-"),
            "stop_reason": stop_reason,
            "blocked_by_plugin_code": None,
        }

    if skipped_item:
        reason = str(skipped_item.get("reason") or "-")
        return {
            "status": "static_skipped",
            "attempted": False,
            "ok": False,
            "message": reason,
            "execution_source": "-",
            "matched": False,
            "decision_reason": reason,
            "stop_reason": stop_reason,
            "blocked_by_plugin_code": None,
        }

    return {}


def _as_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
