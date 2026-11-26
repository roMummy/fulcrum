"""群聊周报式总结插件。

插件保留原有“总结/summary”命令和定时入口，但删除 Dify workflow 路径：模型只
负责生成受限 JSON，Fulcrum 负责统计、校验、缓存和 Kitesurf 图片渲染。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Conversation
from app.services.outbound_batch import enqueue_outbound_jobs_by_conversation
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.messages import PluginMessageAccessError, PluginMessageReader
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore


def _load_local_module(name: str):
    """按当前插件目录加载业务模块，避免目录插件跨插件导入顶层 ``plugins``。"""

    module_path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"{__name__}_{name}", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"群总结模块不可用: {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_report_module = _load_local_module("report")
_renderer_module = _load_local_module("renderer")
MessageSummaryReport = _report_module.MessageSummaryReport
SummaryModelError = _report_module.SummaryModelError
build_structured_report = _report_module.build_structured_report
build_summary_analytics = _report_module.build_summary_analytics
report_from_dict = _report_module.report_from_dict
capture_summary_image = _renderer_module.capture_summary_image
summary_text = _renderer_module.summary_text


DEFAULT_COMMANDS = ("总结", "summary")
DEFAULT_MIN_MESSAGES = 100
DEFAULT_MAX_MESSAGES = 5000
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_TOKENS = 12000
SUMMARY_CACHE_NAMESPACE = "daily_summary"
SUMMARY_CACHE_TTL_SECONDS = 3 * 24 * 60 * 60
SCHEDULE_ACTIONS = {"daily_summary_prepare", "daily_summary"}


@dataclass(frozen=True)
class SummaryConfig:
    """保存当前租户独立的 OpenAI-compatible 模型和业务开关。"""

    llm_api_base: str
    llm_api_key: str
    llm_model: str
    llm_api_protocol: str
    llm_timeout_seconds: int
    temperature: float
    max_tokens: int
    reasoning_effort: str
    stream_output: bool
    min_messages: int
    max_messages: int
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
        "debug": {"command_name": command_name, "chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """提供插件重载状态；真正执行仍需要平台创建的可信消息上下文。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群聊消息总结插件已就绪。",
            output={
                "llm_configured": bool(config.llm_api_base and config.llm_model),
                "llm_api_protocol": config.llm_api_protocol,
                "reasoning_effort": config.reasoning_effort,
                "stream_output": config.stream_output,
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
    """读取群消息、计算本地统计、调用结构化模型并生成统一回复。"""

    if request.action == "reload":
        return execute_action(request)
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    config = _load_config(request.config)
    context = request.context
    if not _is_trusted_group_text(context):
        return _ignored_result("group_text_only", "群聊消息总结只支持群聊文本消息。")
    if not config.llm_api_base or not config.llm_model:
        return _business_error(
            "请先在插件设置中配置当前租户自己的模型 API 地址和模型名称。",
            provider_status="llm_not_configured",
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

    filtered_batch = replace(batch, messages=messages)
    analytics = build_summary_analytics(db, filtered_batch)
    stable_user = (
        f"tenant-{context.tenant_id}:binding-{context.binding_id}:conversation-{context.conversation_id}"
        if context
        else "message-summary"
    )
    try:
        report = build_structured_report(
            db=db,
            batch=filtered_batch,
            analytics=analytics,
            api_base=config.llm_api_base,
            api_key=config.llm_api_key,
            model=config.llm_model,
            api_protocol=config.llm_api_protocol,
            timeout_seconds=config.llm_timeout_seconds,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            reasoning_effort=config.reasoning_effort,
            stream=config.stream_output,
            user_id=stable_user,
        )
        # 模型只看消息时间，展示周期必须沿用命令解析出的业务标签。
        report = replace(report, period_label=period.label, date_key=period.date_key)
    except SummaryModelError as exc:
        message = "群聊总结模型暂时不可用，请检查当前租户的模型配置。"
        if exc.code == "invalid_response":
            message = "群聊总结模型返回格式无效，请稍后重试。"
        return _business_error(message, provider_status=exc.code)

    replies, reply_type = _build_summary_replies(config, report)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已完成 {period.label} 的群聊总结。",
        output={
            "provider_status": "completed",
            "report": report.to_dict(),
            "summary": summary_text(report),
            "message_count": report.message_count,
            "participant_count": report.active_member_count,
            "active_member_count": report.active_member_count,
            "period_start": period.start_at.isoformat(),
            "period_end": period.end_at.isoformat(),
            "period_label": period.label,
            "date_key": period.date_key,
            "token_usage": report.token_usage,
            "reply_type": reply_type,
            "replies": replies,
        },
    )


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """预生成或推送昨日群总结，缓存只保存结构化报告和本地统计。"""

    if request.action not in SCHEDULE_ACTIONS:
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的群总结定时动作: {request.action}")
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
        store.put(SUMMARY_CACHE_NAMESPACE, cache_key, state, ttl_seconds=SUMMARY_CACHE_TTL_SECONDS, now=current)
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
    state = cached if isinstance(cached, Mapping) else None
    report: MessageSummaryReport | None = None
    cache_hit = False
    if state is not None and isinstance(state.get("report"), Mapping):
        try:
            report = report_from_dict(state["report"])
            cache_hit = report.date_key == period.date_key
        except (TypeError, ValueError):
            report = None
    if report is None:
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
        store.put(SUMMARY_CACHE_NAMESPACE, cache_key, state, ttl_seconds=SUMMARY_CACHE_TTL_SECONDS, now=current)
        report = report_from_dict(state["report"])

    config = _load_config(request.config)
    replies, _reply_type = _build_summary_replies(config, report)
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
            "message_count": report.message_count,
            "participant_count": report.active_member_count,
            "cache_hit": cache_hit,
            "outbound_job_ids": job_ids,
            "failed_target_count": len(failed_targets),
        },
    )


def _summary_state_from_result(result: PluginExecutionResult) -> dict[str, Any] | None:
    """提取可短期缓存的最小报告，不保存回复、密钥、提示词和原始消息。"""

    if not result.ok or str(result.output.get("provider_status") or "") != "completed":
        return None
    report = result.output.get("report")
    if not isinstance(report, Mapping):
        return None
    try:
        validated = report_from_dict(report)
    except (TypeError, ValueError):
        return None
    return {
        "report": validated.to_dict(),
        "message_count": validated.message_count,
        "participant_count": validated.active_member_count,
        "period_start": str(result.output.get("period_start") or ""),
        "period_end": str(result.output.get("period_end") or ""),
        "date_key": validated.date_key,
        "token_usage": validated.token_usage,
    }


def _scheduled_generation_result(result: PluginExecutionResult, *, action: str) -> PluginExecutionResult:
    """把即时生成结果收敛为不含摘要正文和标准回复的定时任务结果。"""

    provider_status = str(result.output.get("provider_status") or "generation_failed")
    if provider_status == "insufficient_messages":
        return PluginExecutionResult(
            ok=True,
            status="skipped",
            message="昨日群聊消息不足，已跳过自动总结。",
            output={"action": action, "provider_status": provider_status, "message_count": _bounded_int(result.output.get("message_count"), 0, 20000)},
        )
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=result.message or "群聊总结生成失败。",
        output={"action": action, "provider_status": provider_status},
    )


def _build_summary_replies(config: SummaryConfig, report: MessageSummaryReport) -> tuple[list[dict[str, Any]], str]:
    """优先通过 Kitesurf 生成图片，截图失败时使用同一报告文本降级。"""

    if config.render_as_image:
        try:
            image = capture_summary_image(report)
            encoded = base64.b64encode(image.content).decode("ascii")
            return standard_replies([image_reply(base64_data=encoded)]), "image"
        except Exception:
            # Kitesurf 未配置、繁忙或渲染失败不应阻断群总结文字发送。
            pass
    return standard_replies([text_reply(summary_text(report))]), "text"


def _build_scheduled_outbound_payloads(conversation: Conversation, replies: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            payloads.append({**base, "message_type": "text", "content": str(reply["content"]).strip()})
        elif reply_type == "image" and str(reply.get("base64") or "").strip():
            payloads.append({**base, "message_type": "image", "base64": str(reply["base64"]).strip()})
    if not payloads:
        raise ValueError("群聊总结没有可发送的标准回复")
    return payloads


def _summary_cache_key(conversation_id: int, date_key: str) -> str:
    return f"conversation-{int(conversation_id)}:{str(date_key or '').strip()}"


def _resolve_request_command(payload: Mapping[str, Any]) -> tuple[str, str]:
    """从标准消息载荷读取命令；员工手工执行缺少文本时默认执行一次总结。"""

    content = str(payload.get("content") or "").strip()
    command_name, argument = _parse_command_text(content)
    explicit = _normalize_command_name(payload.get("command_name"))
    if explicit in DEFAULT_COMMANDS:
        command_name = explicit
    if not content and not explicit:
        command_name = DEFAULT_COMMANDS[0]
    return command_name, str(payload.get("summary_date") or argument or "").strip()


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
    """按自然日计算指定日期，未指定时默认从昨天零点到当前时间。"""

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
        return SummaryPeriod(start_at, start_at + timedelta(days=1), selected.isoformat(), selected.isoformat(), True)
    yesterday = current.date() - timedelta(days=1)
    start_at = datetime.combine(yesterday, time.min, tzinfo=current.tzinfo)
    return SummaryPeriod(start_at, current, f"{yesterday.isoformat()} 00:00 到现在", yesterday.isoformat(), False)


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

    return PluginExecutionResult(False, "error", message, {"provider_status": provider_status, "replies": standard_replies([text_reply(message)])})


def _ignored_result(reason: str, message: str) -> PluginExecutionResult:
    return PluginExecutionResult(True, "ignored", message, {"provider_status": reason, "replies": []})


def _load_config(raw_config: Mapping[str, Any]) -> SummaryConfig:
    """读取并规整插件配置，旧 Dify 字段不再参与执行。"""

    raw = dict(raw_config)
    min_messages = _bounded_int(raw.get("min_messages"), DEFAULT_MIN_MESSAGES, DEFAULT_MIN_MESSAGES, 20000)
    return SummaryConfig(
        llm_api_base=str(raw.get("llm_api_base") or "").strip().rstrip("/"),
        llm_api_key=str(raw.get("llm_api_key") or "").strip(),
        llm_model=str(raw.get("llm_model") or "").strip(),
        llm_api_protocol=_normalize_choice(raw.get("llm_api_protocol"), {"responses", "chat_completions"}, "responses"),
        llm_timeout_seconds=_bounded_int(raw.get("llm_timeout_seconds"), DEFAULT_TIMEOUT_SECONDS, 10, 180),
        temperature=_bounded_float(raw.get("temperature"), 0.3, 0.0, 2.0),
        max_tokens=_bounded_int(raw.get("max_tokens"), DEFAULT_MAX_TOKENS, 400, 32000),
        reasoning_effort=_normalize_choice(raw.get("reasoning_effort"), {"none", "low", "medium", "high"}, "medium"),
        stream_output=_as_bool(raw.get("stream_output"), default=True),
        min_messages=min_messages,
        max_messages=_bounded_int(raw.get("max_messages"), DEFAULT_MAX_MESSAGES, min_messages, 20000),
        render_as_image=_as_bool(raw.get("render_as_image"), default=True),
    )


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


def _normalize_choice(value: Any, choices: set[str], default: str) -> str:
    """只接受管理后台声明的枚举，避免配置值拼接到上游请求。"""

    normalized = str(value or "").strip().lower()
    return normalized if normalized in choices else default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))
