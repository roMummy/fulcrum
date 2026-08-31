from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Channel, Conversation, Message, TaskJob
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.task_queue import FINAL_STATUSES
from app.services.workwechat_cloud_poll import resolve_outbound_job_type


PLUGIN_CODE = "message_push_task"
COMMAND = "定时推送"
RELATED_TYPE = "message_push_task_binding"
MAX_CONTENT_LENGTH = 4000
LIST_LIMIT = 10
LIST_SCAN_LIMIT = 100


@dataclass(frozen=True)
class TrustedPushContext:
    """保存经过数据库交叉验证的当前插件绑定、群会话和入站消息。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message


@dataclass(frozen=True)
class PushCommand:
    """表示从真实群消息解析出的唯一业务命令。"""

    operation: str
    schedule_at: datetime | None = None
    content: str = ""
    job_id: int | None = None


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行时只承担健康检查，禁止运维接口伪造群消息创建任务。"""

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="定时推送插件运行正常。",
            output={"command": COMMAND},
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在目标群聊中使用定时推送命令。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """只在当前可信群会话中创建、查看或取消文本推送任务。"""

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    trusted, error = _require_trusted_group_message(db, request.context)
    if error is not None or trusted is None:
        return error or _error_result("当前群聊消息上下文无效。", "trusted_context_required")

    command, parse_error = _parse_command(str(trusted.message.content or ""))
    if parse_error is not None or command is None:
        return parse_error or _help_result()
    if command.operation == "list":
        return _list_tasks(db, trusted)
    if command.operation == "cancel" and command.job_id is not None:
        return _cancel_task(db, trusted, command.job_id)
    if command.operation == "create" and command.schedule_at is not None:
        return _create_task(db, trusted, command)
    return _help_result()


def _create_task(db: Session, trusted: TrustedPushContext, command: PushCommand) -> PluginExecutionResult:
    """按绑定、会话、消息主键和计划时间幂等创建一个纯文本出站任务。"""

    schedule_at = ensure_shanghai_datetime(command.schedule_at).replace(microsecond=0)
    if schedule_at <= now_shanghai():
        return _error_result("计划时间必须晚于当前时间。", "schedule_time_expired")
    content = command.content.strip()
    if not content:
        return _error_result("推送内容不能为空。", "content_required")
    if len(content) > MAX_CONTENT_LENGTH:
        return _error_result(f"推送内容不能超过 {MAX_CONTENT_LENGTH} 个字符。", "content_too_long")

    dedupe_key = (
        f"message-push:{int(trusted.binding.id)}:{int(trusted.conversation.id)}:"
        f"{int(trusted.message.id)}:{int(schedule_at.timestamp())}"
    )
    existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
    if existing is None:
        try:
            # 唯一键负责跨进程并发仲裁；SAVEPOINT 仅回滚重复插入，不破坏当前
            # 插件执行事务中的消息记录和其他业务数据。
            with db.begin_nested():
                existing = enqueue_outbound_job(
                    db,
                    conversation=trusted.conversation,
                    channel=trusted.channel,
                    payload={
                        "external_id": str(trusted.conversation.external_id or "").strip(),
                        "receiver_id": str(trusted.conversation.external_id or "").strip(),
                        "chat_type": "group",
                        "message_type": "text",
                        "content": content,
                    },
                    plugin_code=PLUGIN_CODE,
                    related_type=RELATED_TYPE,
                    related_id=int(trusted.binding.id),
                    priority=80,
                    available_at=schedule_at,
                    dedupe_key=dedupe_key,
                    # 消息发送属于不可确认远端副作用，同一任务失败后不自动重发，
                    # 防止 provider 已发送但回执丢失时产生重复提醒。
                    max_attempts=1,
                )
        except IntegrityError:
            existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
            if existing is None:
                raise
    db.commit()

    reply_text = f"定时推送已创建。\n任务 ID：{int(existing.id)}\n计划时间：{schedule_at.isoformat()}\n内容：{content}"
    return _text_result(
        reply_text,
        "scheduled",
        job_id=int(existing.id),
        schedule_at=schedule_at.isoformat(),
        conversation_id=int(trusted.conversation.id),
    )


def _list_tasks(db: Session, trusted: TrustedPushContext) -> PluginExecutionResult:
    """只展示当前绑定在当前群创建的定时推送，不扫描其他插件任务。"""

    expected_job_type = resolve_outbound_job_type(trusted.channel)
    candidates = db.scalars(
        select(TaskJob)
        .where(
            TaskJob.job_type == expected_job_type,
            TaskJob.related_type == RELATED_TYPE,
            TaskJob.related_id == int(trusted.binding.id),
        )
        .order_by(desc(TaskJob.id))
        .limit(LIST_SCAN_LIMIT)
    ).all()
    items: list[dict[str, Any]] = []
    for job in candidates:
        payload = _parse_json(job.payload_json)
        if not _is_current_conversation_job(payload, trusted.conversation.id):
            continue
        message_payload = payload.get("payload")
        if not isinstance(message_payload, dict) or str(message_payload.get("message_type") or "") != "text":
            continue
        items.append(
            {
                "job_id": int(job.id),
                "status": str(job.status or ""),
                "available_at": job.available_at.isoformat() if job.available_at else "",
                "content": str(message_payload.get("content") or "").strip(),
            }
        )
        if len(items) >= LIST_LIMIT:
            break

    if not items:
        return _text_result("当前群还没有定时推送任务。", "empty", tasks=[])
    lines = ["当前群最近的定时推送："]
    for item in items:
        lines.append(f"#{item['job_id']} {item['status']} {item['available_at']} {item['content']}")
    return _text_result("\n".join(lines), "listed", tasks=items)


def _cancel_task(db: Session, trusted: TrustedPushContext, job_id: int) -> PluginExecutionResult:
    """取消任务前同时校验当前租户会话、绑定、群会话和插件载荷。"""

    expected_job_type = resolve_outbound_job_type(trusted.channel)
    job = db.scalar(
        select(TaskJob).where(
            TaskJob.id == int(job_id),
            TaskJob.job_type == expected_job_type,
            TaskJob.related_type == RELATED_TYPE,
            TaskJob.related_id == int(trusted.binding.id),
        )
    )
    payload = _parse_json(job.payload_json) if job is not None else {}
    if job is None or not _is_current_conversation_job(payload, trusted.conversation.id):
        return _error_result("未找到当前群可取消的定时推送任务。", "task_not_found")
    if str(job.status or "") in FINAL_STATUSES:
        return _error_result(f"任务当前状态为 {job.status}，不能取消。", "task_finished")
    if str(job.status or "") != "pending":
        return _error_result("任务正在发送，不能取消。", "task_running")

    job.status = "cancelled"
    job.finished_at = now_shanghai()
    db.commit()
    return _text_result(f"定时推送 #{int(job.id)} 已取消。", "cancelled", job_id=int(job.id))


def _require_trusted_group_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedPushContext | None, PluginExecutionResult | None]:
    """交叉验证绑定、渠道、会话和入站消息，业务载荷不参与身份定位。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "group"
        or context.message_type != "text"
    ):
        return None, _error_result("请在群聊中发送文本定时推送命令。", "trusted_context_required")

    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or plugin_code != PLUGIN_CODE
        or channel is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or not channel.is_enabled
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or str(conversation.chat_type or "") != "group"
        or not str(conversation.external_id or "").strip()
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "") != "in"
        or str(message.message_type or "") != "text"
        or (context.sender_id is not None and str(context.sender_id).strip() != str(message.sender_id or "").strip())
    ):
        return None, _error_result("当前群聊消息上下文无效。", "trusted_context_required")
    return TrustedPushContext(context, binding, channel, conversation, message), None


def _parse_command(content: str) -> tuple[PushCommand | None, PluginExecutionResult | None]:
    """解析唯一中文命令格式，删除旧别名和技术参数兼容。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].strip()
    parts = normalized.split(maxsplit=2)
    if len(parts) < 2 or parts[0] != COMMAND:
        return None, _help_result()
    operation = parts[1]
    if operation == "列表" and len(parts) == 2:
        return PushCommand(operation="list"), None
    if operation == "取消" and len(parts) == 3:
        try:
            job_id = int(parts[2].strip())
        except ValueError:
            job_id = 0
        if job_id > 0:
            return PushCommand(operation="cancel", job_id=job_id), None
        return None, _error_result("任务 ID 必须是正整数。", "job_id_invalid")
    if operation == "创建" and len(parts) == 3:
        schedule_text, separator, push_content = parts[2].strip().partition(" ")
        schedule_at = _parse_schedule_time(schedule_text)
        if schedule_at is None:
            return None, _error_result("时间格式无效，请使用带时区的 ISO 时间。", "schedule_time_invalid")
        if not separator or not push_content.strip():
            return None, _error_result("请填写需要推送的文本内容。", "content_required")
        return PushCommand(operation="create", schedule_at=schedule_at, content=push_content.strip()), None
    return None, _help_result()


def _parse_schedule_time(value: str) -> datetime | None:
    """只接受显式带时区的 ISO 时间，避免不同部署节点产生时区歧义。"""

    normalized = str(value or "").strip().replace("Z", "+00:00")
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return ensure_shanghai_datetime(parsed).replace(microsecond=0)


def _is_current_conversation_job(payload: dict[str, Any], conversation_id: int) -> bool:
    """确认任务载荷仍属于本插件和当前群会话。"""

    try:
        payload_conversation_id = int(payload.get("conversation_id") or 0)
    except (TypeError, ValueError):
        return False
    return str(payload.get("plugin_code") or "") == PLUGIN_CODE and payload_conversation_id == int(conversation_id)


def _parse_json(raw: str | None) -> dict[str, Any]:
    """安全读取任务载荷，损坏数据视为非本插件任务。"""

    normalized = str(raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _help_result() -> PluginExecutionResult:
    """返回面向群用户的最小命令说明。"""

    text = (
        "定时推送命令：\n"
        "定时推送 创建 2026-09-01T10:00:00+08:00 提醒内容\n"
        "定时推送 列表\n"
        "定时推送 取消 任务ID"
    )
    return _text_result(text, "help")


def _text_result(text: str, provider_status: str, **output: Any) -> PluginExecutionResult:
    """构造统一文本回复，避免各命令重复拼装响应结构。"""

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text,
        output={
            "provider_status": provider_status,
            **output,
            "replies": standard_replies([text_reply(text)]),
        },
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """构造不含底层队列和 provider 细节的业务错误。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status},
    )
