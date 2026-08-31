from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Conversation, Plugin, TaskJob
from app.services.plugin_runtime.contracts import PluginExecutionContext
from app.services.plugin_runtime.service import execute_binding_scheduled_action
from app.services.task_queue import PLUGIN_SCHEDULED_ACTION_JOB, enqueue_job


SCHEDULED_RELATED_PREFIX = "plugin_action:"
MAX_ACTION_KEY_LENGTH = 32
MAX_SCHEDULE_PARAMETERS_BYTES = 16 * 1024
ACTION_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]*$")
ALLOWED_JOB_PAYLOAD_KEYS = {"binding_id", "action_key", "conversation_id", "parameters"}
FORBIDDEN_PARAMETER_KEYS = {
    "tenant_id",
    "binding_id",
    "bot_id",
    "channel_id",
    "conversation_id",
    "message_id",
    "raw_event_id",
}
SENSITIVE_PARAMETER_KEYWORDS = ("password", "secret", "token", "api_key", "apikey", "authorization", "credential")


class PluginSchedulerError(ValueError):
    """插件计划、任务载荷或执行结果不符合统一调度约束。"""


@dataclass(frozen=True)
class PluginScheduledActionSpec:
    """描述一个需要幂等入队的插件动作。"""

    action_key: str
    scheduled_for: datetime
    parameters: Mapping[str, Any]
    conversation_id: int | None = None
    high_side_effect: bool = False
    priority: int = 60


def enqueue_plugin_scheduled_action(
    db: Session,
    binding: BotPlugin,
    *,
    action_key: str,
    scheduled_for: datetime,
    parameters: Mapping[str, Any] | None = None,
    conversation_id: int | None = None,
    high_side_effect: bool = False,
    priority: int = 60,
) -> tuple[TaskJob, bool]:
    """按绑定、动作和计划秒级时间幂等创建插件任务。"""

    tenant_id = require_tenant_id()
    if int(binding.tenant_id) != tenant_id:
        raise PluginSchedulerError("插件绑定与当前租户会话不一致")
    normalized_action = normalize_action_key(action_key)
    normalized_time = ensure_shanghai_datetime(scheduled_for).replace(microsecond=0)
    normalized_parameters = validate_schedule_parameters(parameters or {})
    normalized_conversation_id = _coerce_optional_positive_int(conversation_id)
    if conversation_id is not None and normalized_conversation_id is None:
        raise PluginSchedulerError("目标会话编号不合法")
    if normalized_conversation_id is not None:
        conversation = db.get(Conversation, normalized_conversation_id)
        if conversation is None or int(conversation.bot_id) != int(binding.bot_id):
            raise PluginSchedulerError("目标会话不属于当前插件机器人")

    related_type = f"{SCHEDULED_RELATED_PREFIX}{normalized_action}"
    dedupe_key = _build_schedule_dedupe_key(
        binding_id=int(binding.id),
        action_key=normalized_action,
        scheduled_for=normalized_time,
    )
    existing = db.scalar(
        select(TaskJob)
        .where(
            TaskJob.job_type == PLUGIN_SCHEDULED_ACTION_JOB,
            TaskJob.dedupe_key == dedupe_key,
        )
        .order_by(TaskJob.id.asc())
        .limit(1)
    )
    if existing is not None:
        return existing, False

    payload: dict[str, Any] = {
        "binding_id": int(binding.id),
        "action_key": normalized_action,
        "parameters": normalized_parameters,
    }
    if normalized_conversation_id is not None:
        payload["conversation_id"] = normalized_conversation_id
    try:
        # 租户唯一键承担跨进程并发仲裁。SAVEPOINT 只回滚重复插入，不破坏调用方
        # 在同一租户事务中已经创建的其他计划任务。
        with db.begin_nested():
            job = enqueue_job(
                db,
                job_type=PLUGIN_SCHEDULED_ACTION_JOB,
                payload=payload,
                priority=max(1, min(int(priority), 1000)),
                related_type=related_type,
                related_id=int(binding.id),
                dedupe_key=dedupe_key,
                max_attempts=1 if high_side_effect else 3,
                available_at=normalized_time,
            )
        return job, True
    except IntegrityError:
        existing = db.scalar(
            select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).order_by(TaskJob.id.asc()).limit(1)
        )
        if existing is None:
            raise PluginSchedulerError("插件计划任务并发入队后无法读取幂等记录")
        return existing, False


def schedule_due_plugin_actions(db: Session, *, now: datetime | None = None) -> list[TaskJob]:
    """扫描当前租户已启用绑定，并为已到期计划创建统一任务。"""

    current = ensure_shanghai_datetime(now)
    bindings = db.scalars(
        select(BotPlugin)
        .join(Plugin, Plugin.id == BotPlugin.plugin_id)
        .where(
            BotPlugin.status == "enabled",
            Plugin.code.in_(("daily_news", "douyu")),
        )
        .order_by(BotPlugin.id.asc())
    ).all()
    created_jobs: list[TaskJob] = []
    for binding in bindings:
        if binding.bot is None or not binding.bot.is_enabled:
            continue
        config = _parse_json_object(binding.config_json)
        plugin_code = str(binding.plugin.code if binding.plugin else "").strip()
        for spec in _build_plugin_schedule_specs(plugin_code, config, current):
            job, created = enqueue_plugin_scheduled_action(
                db,
                binding,
                action_key=spec.action_key,
                scheduled_for=spec.scheduled_for,
                parameters=spec.parameters,
                conversation_id=spec.conversation_id,
                high_side_effect=spec.high_side_effect,
                priority=spec.priority,
            )
            if created:
                created_jobs.append(job)
    return created_jobs


def process_plugin_scheduled_action_job(
    db: Session,
    job: TaskJob,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """从任务租户归属重建绑定、目标会话、密钥和可信执行上下文。"""

    if str(job.job_type or "") != PLUGIN_SCHEDULED_ACTION_JOB:
        raise PluginSchedulerError("任务类型不是插件定时动作")
    unexpected_keys = set(payload) - ALLOWED_JOB_PAYLOAD_KEYS
    if unexpected_keys:
        raise PluginSchedulerError("插件定时任务包含未允许的载荷字段")

    binding_id = _coerce_optional_positive_int(payload.get("binding_id"))
    if binding_id is None:
        raise PluginSchedulerError("插件定时任务缺少绑定编号")
    binding = db.get(BotPlugin, binding_id)
    if binding is None or int(binding.tenant_id) != int(job.tenant_id):
        raise PluginSchedulerError("插件绑定不存在或不属于任务租户")
    if binding.status != "enabled" or binding.bot is None or not binding.bot.is_enabled:
        raise PluginSchedulerError("插件绑定或机器人未启用")

    action_key = normalize_action_key(str(payload.get("action_key") or ""))
    expected_related_type = f"{SCHEDULED_RELATED_PREFIX}{action_key}"
    if str(job.related_type or "") != expected_related_type or int(job.related_id or 0) != binding_id:
        raise PluginSchedulerError("插件定时任务幂等定位与载荷不一致")
    parameters = validate_schedule_parameters(payload.get("parameters") or {})

    conversation_id = _coerce_optional_positive_int(payload.get("conversation_id"))
    conversation = db.get(Conversation, conversation_id) if conversation_id is not None else None
    if conversation_id is not None and (
        conversation is None or int(conversation.bot_id) != int(binding.bot_id)
    ):
        raise PluginSchedulerError("插件定时任务目标会话不属于当前机器人")

    context = PluginExecutionContext(
        tenant_id=int(job.tenant_id),
        binding_id=int(binding.id),
        bot_id=int(binding.bot_id),
        channel_id=int(conversation.channel_id) if conversation and conversation.channel_id else None,
        conversation_id=int(conversation.id) if conversation else None,
        channel_type=(str(conversation.channel_type or "").strip() or None) if conversation else None,
        chat_type=(str(conversation.chat_type or "").strip() or None) if conversation else None,
    )
    result = execute_binding_scheduled_action(
        db,
        binding,
        action_key,
        payload=parameters,
        context=context,
    )
    if result is None:
        raise PluginSchedulerError("插件运行时不支持定时动作")
    if not result.ok:
        raise PluginSchedulerError(result.message or "插件定时动作执行失败")

    binding.last_execution_at = now_shanghai()
    binding.last_execution_status = result.status
    binding.last_execution_summary = result.message
    return {
        "binding_id": int(binding.id),
        "plugin_code": str(binding.plugin.code if binding.plugin else ""),
        "action_key": action_key,
        "status": result.status,
        "summary": result.message,
        "output": sanitize_scheduled_result(result.output),
    }


def validate_schedule_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """只允许可序列化的非敏感业务参数进入任务载荷。"""

    if not isinstance(parameters, Mapping):
        raise PluginSchedulerError("插件定时任务参数必须是 JSON 对象")
    normalized = dict(parameters)
    _validate_parameter_tree(normalized)
    try:
        serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PluginSchedulerError("插件定时任务参数不可序列化") from exc
    if len(serialized.encode("utf-8")) > MAX_SCHEDULE_PARAMETERS_BYTES:
        raise PluginSchedulerError("插件定时任务参数超过 16 KiB")
    return normalized


def sanitize_scheduled_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """清除定时任务结果中的疑似凭证字段，避免任务表长期保存敏感信息。"""

    if not isinstance(value, Mapping):
        return {}
    sanitized = _sanitize_result_value(dict(value))
    return sanitized if isinstance(sanitized, dict) else {}


def normalize_action_key(action_key: str) -> str:
    normalized = str(action_key or "").strip().lower()
    if (
        not normalized
        or len(normalized) > MAX_ACTION_KEY_LENGTH
        or not ACTION_KEY_PATTERN.fullmatch(normalized)
    ):
        raise PluginSchedulerError("插件动作代码格式不合法")
    return normalized


def _build_plugin_schedule_specs(
    plugin_code: str,
    config: Mapping[str, Any],
    current: datetime,
) -> tuple[PluginScheduledActionSpec, ...]:
    """把现有两个定时插件收敛为统一动作计划，后续插件继续在此注册。"""

    if plugin_code == "daily_news":
        today = current.strftime("%Y-%m-%d")
        if str(config.get("last_push_date") or "").strip() == today:
            return ()
        hour, minute = _parse_clock_time(str(config.get("push_time") or "08:30"))
        scheduled_for = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if current < scheduled_for:
            return ()
        return (
            PluginScheduledActionSpec(
                action_key="push_daily",
                scheduled_for=scheduled_for,
                parameters={},
                high_side_effect=True,
                priority=70,
            ),
        )
    if plugin_code == "douyu":
        if not _mapping_has_values(config.get("room_subscriptions")) and not _mapping_has_values(
            config.get("yuba_subscriptions")
        ):
            return ()
        interval = max(30, min(_coerce_int(config.get("poll_interval_seconds"), 60), 3600))
        timestamp = math.floor(current.timestamp() / interval) * interval
        scheduled_for = datetime.fromtimestamp(timestamp, tz=current.tzinfo)
        return (
            PluginScheduledActionSpec(
                action_key="poll_subscriptions",
                scheduled_for=scheduled_for,
                parameters={},
                high_side_effect=True,
                priority=65,
            ),
        )
    return ()


def _build_schedule_dedupe_key(*, binding_id: int, action_key: str, scheduled_for: datetime) -> str:
    """生成租户内稳定且可排查的计划任务幂等键。"""

    timestamp = int(ensure_shanghai_datetime(scheduled_for).timestamp())
    return f"plugin:{binding_id}:{action_key}:{timestamp}"


def _validate_parameter_tree(value: Any, *, key_name: str = "") -> None:
    if key_name:
        lowered = key_name.lower()
        if lowered in FORBIDDEN_PARAMETER_KEYS:
            raise PluginSchedulerError("插件定时任务参数不能携带平台身份字段")
        if any(keyword in lowered for keyword in SENSITIVE_PARAMETER_KEYWORDS):
            raise PluginSchedulerError("插件定时任务参数不能携带敏感配置")
    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            _validate_parameter_tree(child_value, key_name=str(child_key))
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _validate_parameter_tree(child)
        return
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise PluginSchedulerError("插件定时任务参数包含非 JSON 类型")


def _sanitize_result_value(value: Any, *, key_name: str = "") -> Any:
    lowered = key_name.lower()
    if lowered and any(keyword in lowered for keyword in SENSITIVE_PARAMETER_KEYWORDS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(key): _sanitize_result_value(child, key_name=str(key)) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_result_value(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "").strip() or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_clock_time(value: str) -> tuple[int, int]:
    hour_text, separator, minute_text = str(value or "").strip().partition(":")
    if not separator:
        return 8, 30
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        return 8, 30
    return max(0, min(hour, 23)), max(0, min(minute, 59))


def _mapping_has_values(value: Any) -> bool:
    return isinstance(value, Mapping) and any(
        isinstance(items, (list, tuple)) and any(str(item).strip() for item in items)
        for items in value.values()
    )


def _coerce_optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
