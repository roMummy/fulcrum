from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.markdown_to_image import convert_md_str_to_image_sync
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Conversation
from app.services.outbound_batch import enqueue_outbound_jobs_by_conversation
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse
from app.services.plugin_runtime.messages import PluginMessageAccessError, PluginMessageBatch, PluginMessageReader
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore


DEFAULT_COMMANDS = ("总结", "summary")
DEFAULT_MIN_MESSAGES = 100
DEFAULT_MAX_MESSAGES = 1000
DEFAULT_TIMEOUT_SECONDS = 60
MAX_SUMMARY_TEXT_LENGTH = 18_000
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUMMARY_IMAGE_ROOT = PROJECT_ROOT / "data" / "temp" / "md2image"
SUMMARY_CACHE_NAMESPACE = "daily_summary"
SUMMARY_CACHE_TTL_SECONDS = 3 * 24 * 60 * 60
SCHEDULE_ACTIONS = {"daily_summary_prepare", "daily_summary"}


@dataclass(frozen=True)
class SummaryConfig:
    """保存租户群总结配置，Dify 地址和密钥均由当前绑定提供。"""

    api_base: str
    api_key: str
    min_messages: int
    max_messages: int
    request_timeout_seconds: int
    render_as_image: bool


@dataclass(frozen=True)
class SummaryPeriod:
    """描述一次手工总结使用的上海时区时间范围。"""

    start_at: datetime
    end_at: datetime
    label: str
    date_key: str
    custom_day: bool


def matches_event(context) -> dict[str, object]:
    """只匹配群聊文本中的总结命令，私聊和其他消息不进入运行时。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "").strip()
    command_name, _argument = _parse_command_text(content)
    matched = chat_type == "group" and message_type == "text" and command_name in DEFAULT_COMMANDS
    return {
        "matched": matched,
        "reason": "summary_command_hit" if matched else "summary_group_command_required",
        "debug": {
            "command_name": command_name,
            "chat_type": chat_type,
            "message_type": message_type,
        },
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """保留无数据库依赖的运行状态入口；实际总结必须走消息动作接口。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群聊消息总结插件已就绪。",
            output={
                "provider_configured": bool(config.api_base and config.api_key),
                "commands": list(DEFAULT_COMMANDS),
                "min_messages": config.min_messages,
                "max_messages": config.max_messages,
                "render_as_image": config.render_as_image,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return PluginExecutionResult(
        ok=False,
        status="error",
        message="群聊总结必须通过平台消息运行时执行。",
        output={"provider_status": "message_runtime_required", "replies": []},
    )


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """读取当前租户群消息、调用 Dify 工作流并生成通用渠道回复。"""

    if request.action == "reload":
        return execute_action(request)
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    config = _load_config(request.config)
    context = request.context
    if not _is_trusted_group_text(context):
        return _ignored_result("group_text_only", "群聊消息总结只支持群聊文本消息。")
    if not config.api_base or not config.api_key:
        return _business_error(
            "请先在插件设置中配置当前租户自己的 Dify 地址和 API Key。",
            provider_status="not_configured",
        )

    command_name, date_argument = _resolve_request_command(request.payload)
    if command_name not in DEFAULT_COMMANDS:
        return _ignored_result("summary_command_required", "请使用“总结”或“summary”命令。")
    try:
        period = _resolve_summary_period(date_argument, now=now_shanghai())
    except ValueError as exc:
        return _business_error(str(exc), provider_status="invalid_date")

    try:
        batch = PluginMessageReader(db, context).read(
            start_at=period.start_at,
            end_at=period.end_at,
            limit=config.max_messages,
        )
    except PluginMessageAccessError:
        # 归属错误只返回统一业务提示，不能把租户、机器人或数据库定位细节发到群里。
        return _business_error("当前群聊无法读取消息记录，请检查机器人和群聊绑定。", provider_status="access_denied")

    messages = tuple(item for item in batch.messages if not _is_summary_command(item.content))
    if len(messages) < config.min_messages:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=f"{period.label}消息不足，未生成总结。",
            output={
                "provider_status": "insufficient_messages",
                "message_count": len(messages),
                "min_messages": config.min_messages,
                "period_start": period.start_at.isoformat(),
                "period_end": period.end_at.isoformat(),
                "period_label": period.label,
                "replies": standard_replies(
                    [text_reply(f"{period.label}只有 {len(messages)} 条可总结消息，至少需要 {config.min_messages} 条。")]
                ),
            },
        )

    chat_content = "\n".join(item.formatted_text for item in messages)
    participant_count = len(
        {
            item.sender_id or item.sender_name
            for item in messages
            if str(item.direction or "").strip().lower() == "in" and (item.sender_id or item.sender_name)
        }
    )
    try:
        response = _request_dify_summary(
            request,
            config,
            batch=batch,
            period=period,
            chat_content=chat_content,
            message_count=len(messages),
            participant_count=participant_count,
        )
    except PluginHttpError:
        return _business_error("Dify 服务暂时不可用，请稍后重试。", provider_status="request_failed")

    if response.status_code < 200 or response.status_code >= 300:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="Dify 服务返回错误，请检查当前租户的服务配置。",
            output={
                "provider_status": "http_error",
                "http_status": int(response.status_code),
                "replies": [],
            },
        )

    try:
        provider_payload = response.json()
        summary_text, token_usage = _extract_workflow_result(provider_payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _business_error("Dify 工作流未返回有效的 text 结果。", provider_status="invalid_response")

    replies, reply_type = _build_summary_replies(
        config,
        summary_text=summary_text,
        conversation_title=batch.conversation_title,
        period_label=period.label,
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已完成 {period.label} 的群聊总结。",
        output={
            "provider_status": "completed",
            "summary": summary_text,
            "message_count": len(messages),
            "participant_count": participant_count,
            "period_start": period.start_at.isoformat(),
            "period_end": period.end_at.isoformat(),
            "period_label": period.label,
            "date_key": period.date_key,
            "token_usage": token_usage,
            "reply_type": reply_type,
            "replies": replies,
        },
    )


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """预生成或推送昨日群总结，所有数据都从任务所属租户会话重新读取。"""

    if request.action not in SCHEDULE_ACTIONS:
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的群总结定时动作: {request.action}",
        )
    context = request.context
    if context is None or context.conversation_id is None:
        return PluginExecutionResult(ok=False, status="error", message="群总结定时任务缺少目标群聊。")
    binding = db.get(BotPlugin, int(context.binding_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
    ):
        return PluginExecutionResult(ok=False, status="error", message="群总结插件绑定或机器人未启用。")
    if (
        conversation is None
        or int(conversation.bot_id) != int(binding.bot_id)
        or str(conversation.chat_type or "").strip().lower() != "group"
    ):
        return PluginExecutionResult(ok=False, status="error", message="群总结定时任务目标不是当前机器人的群聊。")

    current = now_shanghai()
    yesterday = (current.date() - timedelta(days=1)).isoformat()
    period = _resolve_summary_period(yesterday, now=current)
    cache_key = _summary_cache_key(int(conversation.id), period.date_key)
    store = PluginStateStore(db, context)

    if request.action == "daily_summary_prepare":
        # 预生成阶段只需要摘要文本和统计，不渲染图片。这样凌晨任务不会把大块
        # base64 写入内存或状态，早上推送再按当前渠道统一生成表现形式。
        generated = execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config={**dict(request.config), "render_as_image": False},
                payload={"command_name": "总结", "summary_date": period.date_key},
                context=context,
            ),
        )
        state = _summary_state_from_result(generated)
        if state is None:
            return _scheduled_generation_result(generated, action="prepare")
        store.put(
            SUMMARY_CACHE_NAMESPACE,
            cache_key,
            state,
            ttl_seconds=SUMMARY_CACHE_TTL_SECONDS,
            now=current,
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=f"已预生成 {period.date_key} 的群聊总结。",
            output={
                "date_key": period.date_key,
                "conversation_id": int(conversation.id),
                "message_count": int(state["message_count"]),
                "participant_count": int(state["participant_count"]),
                "cache_status": "prepared",
            },
        )

    cached = store.get(SUMMARY_CACHE_NAMESPACE, cache_key, now=current)
    cache_hit = cached is not None and bool(str(cached.get("summary") or "").strip())
    if cache_hit:
        state = cached
        config = _load_config(request.config)
        replies, _reply_type = _build_summary_replies(
            config,
            summary_text=str(state["summary"]),
            conversation_title=str(conversation.title or conversation.external_id or "群聊").strip() or "群聊",
            period_label=period.label,
        )
    else:
        generated = execute_message_action(
            db,
            PluginExecutionRequest(
                action="execute",
                config=request.config,
                payload={"command_name": "总结", "summary_date": period.date_key},
                context=context,
            ),
        )
        state = _summary_state_from_result(generated)
        if state is None:
            return _scheduled_generation_result(generated, action="push")
        store.put(
            SUMMARY_CACHE_NAMESPACE,
            cache_key,
            state,
            ttl_seconds=SUMMARY_CACHE_TTL_SECONDS,
            now=current,
        )
        replies = [dict(item) for item in generated.output.get("replies", []) if isinstance(item, Mapping)]

    batch_result = enqueue_outbound_jobs_by_conversation(
        db,
        conversations=[conversation],
        plugin_code="message_summary",
        related_type="message_summary_binding",
        related_id=int(binding.id),
        priority=70,
        payload_builder=lambda target, channel: _build_scheduled_outbound_payloads(target, replies),
        log_context={"binding_id": int(binding.id), "date_key": period.date_key},
    )
    failed_targets = list(batch_result["failed_targets"])
    job_ids = [int(item) for item in batch_result["job_ids"]]
    return PluginExecutionResult(
        ok=not failed_targets,
        status="completed" if not failed_targets else "partial",
        message=(
            f"已为 {period.date_key} 的群聊总结创建推送任务。"
            if not failed_targets
            else f"{period.date_key} 的群聊总结推送创建失败。"
        ),
        output={
            "date_key": period.date_key,
            "conversation_id": int(conversation.id),
            "message_count": int(state["message_count"]),
            "participant_count": int(state["participant_count"]),
            "cache_hit": cache_hit,
            "outbound_job_ids": job_ids,
            "failed_target_count": len(failed_targets),
        },
    )


def _request_dify_summary(
    request: PluginExecutionRequest,
    config: SummaryConfig,
    *,
    batch: PluginMessageBatch,
    period: SummaryPeriod,
    chat_content: str,
    message_count: int,
    participant_count: int,
) -> PluginHttpResponse:
    """按来源最新工作流固定输入调用 Dify，不暴露可变字段映射配置。"""

    prompt = (
        f"请总结群聊《{batch.conversation_title}》在 {period.label} 的讨论。"
        f"共有 {message_count} 条消息、{participant_count} 位发言成员。"
        "请剥离无效沟通，提炼关键话题、结论、待办、分歧和有代表性的原话；"
        "不要编造聊天记录中没有的信息，输出可直接发送到群里的中文 Markdown。\n\n"
        f"聊天记录：\n{chat_content}"
    )
    context = request.context
    stable_user = (
        f"tenant-{context.tenant_id}:binding-{context.binding_id}:conversation-{context.conversation_id}"
        if context
        else "message-summary"
    )
    body = {
        "inputs": {
            "query": prompt,
            "group_name": batch.conversation_title,
            "chat_content": chat_content,
            "summary_mode": "markdown",
        },
        "response_mode": "blocking",
        "user": stable_user,
    }
    return PluginHttpClient().post_json(
        f"{config.api_base}/workflows/run",
        body,
        headers={"Authorization": f"Bearer {config.api_key}"},
        connect_timeout_seconds=min(10, config.request_timeout_seconds),
        total_timeout_seconds=config.request_timeout_seconds,
        max_response_bytes=2 * 1024 * 1024,
        max_redirects=2,
    )


def _summary_state_from_result(result: PluginExecutionResult) -> dict[str, Any] | None:
    """从即时执行结果提取可短期缓存的最小摘要，不保存回复和消息正文。"""

    if not result.ok or str(result.output.get("provider_status") or "") != "completed":
        return None
    summary = str(result.output.get("summary") or "").strip()
    if not summary:
        return None
    return {
        "summary": summary,
        "message_count": _bounded_int(result.output.get("message_count"), default=0, minimum=0, maximum=2000),
        "participant_count": _bounded_int(
            result.output.get("participant_count"),
            default=0,
            minimum=0,
            maximum=2000,
        ),
        "period_start": str(result.output.get("period_start") or ""),
        "period_end": str(result.output.get("period_end") or ""),
        "date_key": str(result.output.get("date_key") or ""),
        "token_usage": _bounded_int(
            result.output.get("token_usage"),
            default=0,
            minimum=0,
            maximum=10_000_000,
        ),
    }


def _scheduled_generation_result(result: PluginExecutionResult, *, action: str) -> PluginExecutionResult:
    """把即时生成结果收敛为不含摘要正文和标准回复的定时任务结果。"""

    provider_status = str(result.output.get("provider_status") or "generation_failed")
    if provider_status == "insufficient_messages":
        return PluginExecutionResult(
            ok=True,
            status="skipped",
            message="昨日群聊消息不足，已跳过自动总结。",
            output={
                "action": action,
                "provider_status": provider_status,
                "message_count": _bounded_int(
                    result.output.get("message_count"),
                    default=0,
                    minimum=0,
                    maximum=2000,
                ),
            },
        )
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=result.message or "群聊总结生成失败。",
        output={"action": action, "provider_status": provider_status},
    )


def _build_scheduled_outbound_payloads(
    conversation: Conversation,
    replies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把标准文本或图片回复转换为当前群聊的通用出站载荷。"""

    payloads: list[dict[str, Any]] = []
    base = {
        "external_id": str(conversation.external_id or "").strip(),
        "receiver_id": str(conversation.external_id or "").strip(),
        "chat_type": "group",
    }
    for reply in replies:
        reply_type = str(reply.get("type") or "").strip().lower()
        if reply_type == "text" and str(reply.get("content") or "").strip():
            payloads.append(
                {
                    **base,
                    "message_type": "text",
                    "content": str(reply.get("content") or "").strip(),
                }
            )
        elif reply_type == "image" and str(reply.get("base64") or "").strip():
            payloads.append(
                {
                    **base,
                    "message_type": "image",
                    "base64": str(reply.get("base64") or "").strip(),
                }
            )
    if not payloads:
        raise ValueError("群聊总结没有可发送的标准回复")
    return payloads


def _summary_cache_key(conversation_id: int, date_key: str) -> str:
    return f"conversation-{int(conversation_id)}:{str(date_key or '').strip()}"


def _extract_workflow_result(payload: Any) -> tuple[str, int]:
    """只接受 Dify workflow 的单一 `data.outputs.text` 对外契约。"""

    if not isinstance(payload, Mapping):
        raise ValueError("Dify 响应不是对象")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("Dify 响应缺少 data")
    outputs = data.get("outputs")
    if not isinstance(outputs, Mapping):
        raise ValueError("Dify 响应缺少 outputs")
    summary = _normalize_summary_text(outputs.get("text"))
    if not summary:
        raise ValueError("Dify 响应缺少 text")
    return summary, _bounded_int(data.get("total_tokens"), default=0, minimum=0, maximum=10_000_000)


def _build_summary_replies(
    config: SummaryConfig,
    *,
    summary_text: str,
    conversation_title: str,
    period_label: str,
) -> tuple[list[dict[str, Any]], str]:
    """优先生成通用图片回复；渲染失败时发送同一份摘要文本。"""

    if config.render_as_image:
        image_path: Path | None = None
        try:
            markdown = f"# {conversation_title}\n\n> {period_label}\n\n{summary_text}"
            output_name = f"message-summary-{uuid.uuid4().hex}.png"
            image_path = Path(
                convert_md_str_to_image_sync(markdown, output_name, base_dir=PROJECT_ROOT)
            ).resolve(strict=True)
            # 图片转换器只允许在平台临时目录落盘。读取后立即转成标准 base64 回复，
            # 渠道适配器无需访问服务器路径，临时文件也不会长期占用磁盘。
            image_path.relative_to(SUMMARY_IMAGE_ROOT.resolve())
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            return standard_replies([image_reply(base64_data=encoded)]), "image"
        except Exception:
            pass
        finally:
            if image_path is not None:
                try:
                    image_path.relative_to(SUMMARY_IMAGE_ROOT.resolve())
                    image_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
    return standard_replies([text_reply(summary_text)]), "text"


def _resolve_request_command(payload: Mapping[str, Any]) -> tuple[str, str]:
    """从标准消息载荷读取命令；员工手工执行缺少文本时默认执行一次总结。"""

    content = str(payload.get("content") or "").strip()
    command_name, argument = _parse_command_text(content)
    explicit = _normalize_command_name(payload.get("command_name"))
    if explicit in DEFAULT_COMMANDS:
        command_name = explicit
    if not content and not explicit:
        command_name = DEFAULT_COMMANDS[0]
    date_argument = str(payload.get("summary_date") or argument or "").strip()
    return command_name, date_argument


def _parse_command_text(content: str) -> tuple[str, str]:
    """解析无前缀、斜杠或井号命令，并只返回一个可选日期参数。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    if not normalized:
        return "", ""
    parts = normalized.split(maxsplit=1)
    return _normalize_command_name(parts[0]), parts[1].strip() if len(parts) == 2 else ""


def _normalize_command_name(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].strip()
    return normalized


def _resolve_summary_period(raw_date: str, *, now: datetime) -> SummaryPeriod:
    """按来源规则计算指定自然日，或默认从昨天零点到当前时间。"""

    current = ensure_shanghai_datetime(now)
    normalized = str(raw_date or "").strip()
    if normalized:
        if len(normalized.split()) != 1:
            raise ValueError("日期格式不对，请使用“#总结 2026-04-08”。")
        try:
            selected = date.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("日期格式不对，请使用“#总结 2026-04-08”。") from exc
        if selected > current.date():
            raise ValueError("不能总结未来日期的群聊消息。")
        start_at = datetime.combine(selected, time.min, tzinfo=current.tzinfo)
        end_at = start_at + timedelta(days=1)
        return SummaryPeriod(
            start_at=start_at,
            end_at=end_at,
            label=selected.isoformat(),
            date_key=selected.isoformat(),
            custom_day=True,
        )

    yesterday = current.date() - timedelta(days=1)
    start_at = datetime.combine(yesterday, time.min, tzinfo=current.tzinfo)
    return SummaryPeriod(
        start_at=start_at,
        end_at=current,
        label=f"{yesterday.isoformat()} 00:00 到现在",
        date_key=yesterday.isoformat(),
        custom_day=False,
    )


def _is_summary_command(content: str) -> bool:
    command_name, _argument = _parse_command_text(content)
    return command_name in DEFAULT_COMMANDS


def _is_trusted_group_text(context: PluginExecutionContext | None) -> bool:
    """只信任平台构造的群聊文本上下文，payload 不能提升会话权限。"""

    return bool(
        context
        and context.conversation_id
        and str(context.chat_type or "").strip().lower() == "group"
        and str(context.message_type or "text").strip().lower() == "text"
    )


def _business_error(message: str, *, provider_status: str) -> PluginExecutionResult:
    """把可操作的业务错误转为标准文本回复，不暴露技术异常。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status, "replies": standard_replies([text_reply(message)])},
    )


def _ignored_result(reason: str, message: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="ignored",
        message=message,
        output={"provider_status": reason, "replies": []},
    )


def _load_config(raw_config: Mapping[str, Any]) -> SummaryConfig:
    raw = dict(raw_config)
    min_messages = _bounded_int(
        raw.get("min_messages"),
        default=DEFAULT_MIN_MESSAGES,
        minimum=DEFAULT_MIN_MESSAGES,
        maximum=2000,
    )
    return SummaryConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        min_messages=min_messages,
        max_messages=_bounded_int(
            raw.get("max_messages"),
            default=DEFAULT_MAX_MESSAGES,
            minimum=min_messages,
            maximum=2000,
        ),
        request_timeout_seconds=_bounded_int(
            raw.get("request_timeout_seconds"),
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=10,
            maximum=180,
        ),
        render_as_image=_as_bool(raw.get("render_as_image"), default=True),
    )


def _normalize_summary_text(value: Any) -> str:
    normalized = str(value or "").strip()
    normalized = re.sub(r"^```(?:markdown|md)?\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*```$", "", normalized).strip()
    if len(normalized) > MAX_SUMMARY_TEXT_LENGTH:
        normalized = normalized[:MAX_SUMMARY_TEXT_LENGTH].rstrip() + "\n\n内容较长，已截断。"
    return normalized


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))
