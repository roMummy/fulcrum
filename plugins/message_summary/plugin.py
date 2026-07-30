from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
import time
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.markdown_to_image import convert_md_str_to_image_sync
from app.models.entities import Conversation, Message
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_TIMEOUT_SECONDS = 40
DEFAULT_COMMANDS = ("总结", "日报")


@dataclass(frozen=True)
class SummaryConfig:
    commands: tuple[str, ...]
    max_messages: int
    include_mentions: bool
    enabled: bool
    min_messages: int
    api_base: str
    api_key: str
    app_mode: str
    workflow_input_key: str
    workflow_session_key: str
    request_timeout_seconds: int
    render_as_image: bool


def matches_event(context) -> bool | dict[str, object]:
    content = str(getattr(context, "content", "") or "").strip()
    command_prefix = str(getattr(context, "command_prefix", "/") or "/").strip() or "/"
    normalized = content[len(command_prefix):].strip() if content.startswith(command_prefix) else content
    command_name = normalized.split()[0] if normalized else ""
    matched = command_name in DEFAULT_COMMANDS
    return {
        "matched": matched,
        "reason": "command_hit" if matched else "command_mismatch",
        "debug": {"command_name": command_name},
    }


def handle_event(context):
    payload = {
        "content": getattr(context, "content", ""),
        "command_name": getattr(context, "command_name", ""),
        "conversation_id": getattr(context, "conversation_id", None),
        "conversation_external_id": getattr(context, "conversation_external_id", ""),
        "message_id": getattr(context, "message_id", None),
        "sender_id": getattr(context, "sender_id", ""),
        "normalized_event": dict(getattr(context, "normalized_event", {}) or {}),
        "messages": _extract_context_messages(context),
    }
    config = dict(getattr(context, "binding_config", {}) or {})
    result = execute_action(PluginExecutionRequest(action="execute", config=config, payload=payload))
    return {
        "ok": result.ok,
        "status": result.status,
        "message": result.message,
        "output": dict(result.output),
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message=f"message_summary runtime is ready, window={config.max_messages}",
            output={
                "commands": list(config.commands),
                "max_messages": config.max_messages,
                "include_mentions": config.include_mentions,
                "min_messages": config.min_messages,
                "ai_enabled": bool(config.api_base and config.api_key),
                "mode": config.app_mode,
                "render_as_image": config.render_as_image,
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")
    if not config.enabled:
        return PluginExecutionResult(ok=False, status="disabled", message="message_summary is disabled")
    if not config.api_base or not config.api_key:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="message_summary 缺少 AI 配置，请先配置 api_base 和 api_key",
        )

    command_name = _resolve_command_name(request.payload, config.commands)
    messages = _resolve_messages(request, config)
    if len(messages) < config.min_messages:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message=f"当前可用于总结的消息不足，至少需要 {config.min_messages} 条，当前仅 {len(messages)} 条",
            output={
                "command_name": command_name,
                "count": len(messages),
                "messages": messages,
                "summary": "",
                "mode": "ai",
                "conversation_title": _resolve_conversation_title(request.payload),
                "replies": [],
            },
        )

    conversation_title = _resolve_conversation_title(request.payload)
    try:
        reply_text = _generate_ai_summary(request, config, messages, conversation_title)
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"message_summary AI 请求失败: {exc}")

    if not reply_text:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="message_summary 未获得有效摘要结果",
            output={
                "command_name": command_name,
                "count": len(messages),
                "messages": messages,
                "summary": "",
                "mode": "ai",
                "conversation_title": conversation_title,
                "replies": [],
            },
        )

    replies = _build_replies(
        summary_text=reply_text,
        conversation_title=conversation_title,
        command_name=command_name,
        render_as_image=config.render_as_image,
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"message_summary 已总结 {len(messages)} 条消息",
        output={
            "command_name": command_name,
            "count": len(messages),
            "messages": messages,
            "summary": reply_text,
            "mode": "ai",
            "conversation_title": conversation_title,
            "replies": replies,
        },
    )


def _resolve_messages(request: PluginExecutionRequest, config: SummaryConfig) -> list[str]:
    payload_messages = request.payload.get("messages")
    if isinstance(payload_messages, list):
        normalized = _normalize_messages(payload_messages, include_mentions=config.include_mentions, commands=config.commands)
        if normalized:
            return normalized[-config.max_messages :]

    conversation_id = _coerce_int(request.payload.get("conversation_id"))
    if conversation_id:
        with SessionLocal() as db:
            rows = db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id.desc())
                .limit(config.max_messages)
            ).all()
        items = [
            _format_message_line(
                sender_name=str(row.sender_name or row.sender_id or ("机器人" if row.direction == "out" else "成员")).strip(),
                content=str(row.content or "").strip(),
            )
            for row in reversed(rows)
        ]
        normalized = _normalize_messages(items, include_mentions=config.include_mentions, commands=config.commands)
        if normalized:
            return normalized

    normalized_event = request.payload.get("normalized_event")
    if isinstance(normalized_event, dict):
        details = normalized_event.get("details")
        if isinstance(details, dict):
            for key in ("recent_messages", "history_messages"):
                value = details.get(key)
                if isinstance(value, list):
                    normalized = _normalize_messages(value, include_mentions=config.include_mentions, commands=config.commands)
                    if normalized:
                        return normalized[-config.max_messages :]
    return []


def _generate_ai_summary(
    request: PluginExecutionRequest,
    config: SummaryConfig,
    messages: list[str],
    conversation_title: str,
) -> str:
    prompt = _build_summary_prompt(messages, conversation_title)
    response = _post_json(
        _build_target_url(config.api_base, config.app_mode),
        _build_body(request, config, prompt),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        timeout_seconds=config.request_timeout_seconds,
    )
    http_status = int(response.get("http_status") or 200)
    if http_status >= 400:
        raise RuntimeError(str(response.get("message") or response.get("error") or "AI provider returned error"))
    return _extract_text(response).strip()


def _build_summary_prompt(messages: list[str], conversation_title: str) -> str:
    title = conversation_title or "未命名群聊"
    return (
        "你是一个群聊消息总结助手。请根据下面的聊天记录生成中文摘要。\n"
        "要求：\n"
        "1. 先用 1-2 句话概括整体讨论。\n"
        "2. 再列出 3-5 条关键要点。\n"
        "3. 如有明确结论、待办或分歧，请单独点出。\n"
        "4. 不要编造聊天记录中没有出现的信息。\n"
        "5. 输出简洁、可直接发回群里。\n\n"
        f"会话标题：{title}\n"
        "聊天记录：\n"
        + "\n".join(messages)
    )


def _build_replies(*, summary_text: str, conversation_title: str, command_name: str, render_as_image: bool) -> list[dict[str, str]]:
    if render_as_image:
        try:
            image_markdown = _build_image_markdown(
                summary_text=summary_text,
                conversation_title=conversation_title,
                command_name=command_name,
            )
            output_name = f"message-summary-{int(time.time() * 1000)}.png"
            image_path = convert_md_str_to_image_sync(
                image_markdown,
                output_name,
                base_dir=Path(__file__).resolve().parents[2],
            )
            if image_path:
                return [{"type": "image", "image_path": image_path}]
        except Exception:
            pass
    return _text_reply(summary_text)


def _build_image_markdown(*, summary_text: str, conversation_title: str, command_name: str) -> str:
    title = conversation_title or "群聊"
    normalized_text = str(summary_text or "").strip()
    return (
        f"# {title} {command_name}\n\n"
        f"> AI 自动生成摘要\n\n"
        f"{normalized_text}\n"
    )


def _build_target_url(api_base: str, app_mode: str) -> str:
    if app_mode == "workflow":
        return f"{api_base}/workflows/run"
    return f"{api_base}/chat-messages"


def _build_body(request: PluginExecutionRequest, config: SummaryConfig, prompt: str) -> dict[str, object]:
    user = str(request.payload.get("sender_id") or request.payload.get("user") or "message_summary").strip() or "message_summary"
    if config.app_mode == "workflow":
        inputs = {config.workflow_input_key: prompt}
        session_value = (
            request.payload.get("conversation_external_id")
            or request.payload.get("conversation_id")
            or ""
        )
        if config.workflow_session_key and session_value:
            inputs[config.workflow_session_key] = session_value
        return {
            "inputs": inputs,
            "response_mode": "blocking",
            "user": user,
        }
    return {
        "query": prompt,
        "inputs": {},
        "response_mode": "blocking",
        "conversation_id": str(request.payload.get("conversation_id") or ""),
        "user": user,
    }


def _extract_text(response: dict[str, object]) -> str:
    for key in ("answer", "text", "message"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    data = response.get("data")
    if isinstance(data, dict):
        for key in ("answer", "text", "message"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        for key in ("outputs", "output", "answer", "text"):
            value = data.get(key)
            if isinstance(value, dict):
                for nested_key in ("answer", "text", "result", "content"):
                    nested_value = value.get(nested_key)
                    if nested_value not in (None, ""):
                        return str(nested_value)
                if len(value) == 1:
                    only_value = next(iter(value.values()))
                    if only_value not in (None, ""):
                        return str(only_value)
            if isinstance(value, list):
                collected: list[str] = []
                for item in value:
                    if isinstance(item, str) and item.strip():
                        collected.append(item.strip())
                    elif isinstance(item, dict):
                        for nested_key in ("answer", "text", "result", "content"):
                            nested_value = item.get(nested_key)
                            if nested_value not in (None, ""):
                                collected.append(str(nested_value))
                                break
                if collected:
                    return "\n".join(collected)
            if value not in (None, ""):
                return str(value)
    return ""


def _post_json(url: str, body: dict[str, object], *, headers: dict[str, str], timeout_seconds: int) -> dict[str, object]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urlrequest.urlopen(req, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return _parse_json_like(raw, getattr(response, "status", 200))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return _parse_json_like(raw, exc.code)
    except URLError:
        raise


def _parse_json_like(raw: str, status: int) -> dict[str, object]:
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"http_status": status, "raw": raw}
    if isinstance(parsed, dict):
        parsed.setdefault("http_status", status)
        return parsed
    return {"http_status": status, "data": parsed}


def _resolve_command_name(payload: dict[str, object], commands: tuple[str, ...]) -> str:
    explicit = str(payload.get("command_name") or "").strip()
    if explicit:
        return explicit
    content = str(payload.get("content") or "").strip()
    if not content:
        return commands[0] if commands else "总结"
    normalized = content[1:].strip() if content.startswith("/") else content
    return normalized.split()[0] if normalized else (commands[0] if commands else "总结")


def _resolve_conversation_title(payload: dict[str, object]) -> str:
    normalized_event = payload.get("normalized_event")
    if isinstance(normalized_event, dict):
        conversation = normalized_event.get("conversation")
        if isinstance(conversation, dict):
            title = str(conversation.get("title") or "").strip()
            if title:
                return title
    conversation_id = _coerce_int(payload.get("conversation_id"))
    if conversation_id:
        with SessionLocal() as db:
            conversation = db.get(Conversation, conversation_id)
            if conversation and str(conversation.title or "").strip():
                return str(conversation.title).strip()
    return ""


def _extract_context_messages(context) -> list[str]:
    normalized_event = getattr(context, "normalized_event", {}) or {}
    if isinstance(normalized_event, dict):
        direct = normalized_event.get("messages")
        if isinstance(direct, list):
            return [str(item) for item in direct]
        details = normalized_event.get("details")
        if isinstance(details, dict):
            for key in ("recent_messages", "history_messages"):
                value = details.get(key)
                if isinstance(value, list):
                    return [str(item) for item in value]
    return []


def _normalize_messages(messages: list[object], *, include_mentions: bool, commands: tuple[str, ...]) -> list[str]:
    normalized: list[str] = []
    for item in messages:
        text = str(item or "").strip()
        if not text:
            continue
        if not include_mentions and "@" in text:
            continue
        if _is_summary_meta_message(text, commands):
            continue
        normalized.append(text)
    return normalized


def _format_message_line(*, sender_name: str, content: str) -> str:
    cleaned = re.sub(r"\s+", " ", content).strip()
    return f"{sender_name}: {cleaned}" if cleaned else ""


def _is_summary_meta_message(text: str, commands: tuple[str, ...]) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return True
    raw_content = normalized
    if ": " in normalized:
        raw_content = normalized.split(": ", 1)[1].strip()
    lowered = raw_content.lower()
    for command in commands:
        cmd = str(command).strip()
        if not cmd:
            continue
        if raw_content == cmd or raw_content.startswith(f"/{cmd}") or raw_content.startswith(f"#{cmd}"):
            return True
    summary_markers = (
        "已开始总结",
        "正在生成总结",
        "总结最近消息",
        "消息不足",
        "最近讨论摘要",
    )
    return any(marker in raw_content for marker in summary_markers) or lowered == "summary"


def _load_config(request: PluginExecutionRequest) -> SummaryConfig:
    raw = dict(request.config)
    commands_raw = raw.get("commands")
    commands = tuple(str(item).strip() for item in commands_raw) if isinstance(commands_raw, list) and commands_raw else DEFAULT_COMMANDS
    return SummaryConfig(
        commands=tuple(item for item in commands if item) or DEFAULT_COMMANDS,
        max_messages=_as_int(raw.get("max_messages"), default=100, minimum=1),
        include_mentions=_as_bool(raw.get("include_mentions"), default=True),
        enabled=_as_bool(raw.get("enabled"), default=True),
        min_messages=_as_int(raw.get("min_messages"), default=5, minimum=1),
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        app_mode=_resolve_app_mode(raw),
        workflow_input_key=str(raw.get("workflow_input_key") or "query").strip() or "query",
        workflow_session_key=str(raw.get("workflow_session_key") or "conversation_id").strip(),
        request_timeout_seconds=_resolve_timeout_seconds(raw),
        render_as_image=_as_bool(raw.get("render_as_image"), default=True),
    )


def _resolve_app_mode(config: dict[str, object]) -> str:
    explicit = str(config.get("app_mode") or "").strip().lower()
    if explicit in {"chat", "workflow"}:
        return explicit
    if str(config.get("workflow_id") or "").strip():
        return "workflow"
    return "chat"


def _resolve_timeout_seconds(config: dict[str, object]) -> int:
    raw = config.get("request_timeout_seconds")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(5, value)


def _text_reply(content: str) -> list[dict[str, str]]:
    normalized = str(content).strip()
    return [{"type": "text", "content": normalized}] if normalized else []


def _as_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _as_int(value: object, *, default: int, minimum: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, resolved)


def _coerce_int(value: object) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
