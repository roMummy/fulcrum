from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.entities import Message
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


AUTO_RESPONSE_COMMANDS = {"ai_auto_response", "auto_ai"}
DEFAULT_TIMEOUT_SECONDS = 40


@dataclass(frozen=True)
class AutoResponseConfig:
    enabled: bool
    api_base: str
    api_key: str
    app_mode: str
    workflow_input_key: str
    workflow_session_key: str
    request_timeout_seconds: int
    history_limit: int
    trigger_direct_chat: bool
    trigger_group_mention: bool
    trigger_group_probability: float
    min_group_history: int
    system_prompt: str


def matches_event(context) -> bool | dict[str, object]:
    command_name = str(getattr(context, "command_name", "") or "").strip().lower()
    mentioned_self = bool(getattr(context, "mentioned_self", False))
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()

    if command_name:
        matched = command_name in AUTO_RESPONSE_COMMANDS
        return {
            "matched": matched,
            "reason": "auto_response_command" if matched else "other_command_reserved",
            "debug": {
                "command_name": command_name,
                "chat_type": chat_type,
                "mentioned_self": mentioned_self,
            },
        }

    if chat_type == "direct":
        return {
            "matched": True,
            "reason": "direct_chat_candidate",
            "debug": {
                "command_name": command_name,
                "chat_type": chat_type,
                "mentioned_self": mentioned_self,
            },
        }

    if mentioned_self:
        return {
            "matched": True,
            "reason": "group_mention_candidate",
            "debug": {
                "command_name": command_name,
                "chat_type": chat_type,
                "mentioned_self": mentioned_self,
            },
        }

    return {
        "matched": True,
        "reason": "group_auto_reply_candidate",
        "debug": {
            "command_name": command_name,
            "chat_type": chat_type,
            "mentioned_self": mentioned_self,
        },
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="ai_auto_response runtime is ready",
            output={
                "enabled": config.enabled,
                "api_base": config.api_base,
                "api_key_configured": bool(config.api_key),
                "app_mode": config.app_mode,
                "history_limit": config.history_limit,
                "trigger_direct_chat": config.trigger_direct_chat,
                "trigger_group_mention": config.trigger_group_mention,
                "trigger_group_probability": config.trigger_group_probability,
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")
    if not config.enabled:
        return PluginExecutionResult(ok=False, status="disabled", message="ai_auto_response is disabled")
    if not config.api_base or not config.api_key:
        return PluginExecutionResult(ok=False, status="error", message="ai_auto_response missing api_base or api_key")

    trigger = _resolve_trigger_state(request, config)
    if trigger["skip"]:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=str(trigger["reason"]),
            output={"provider_status": "ignored", "trigger": trigger},
        )

    query = _resolve_query(request)
    if not query:
        return PluginExecutionResult(ok=True, status="completed", message="empty query", output={"provider_status": "ignored"})

    history = _load_recent_history(request, limit=config.history_limit)
    prompt = _build_prompt(config, history=history, query=query, trigger=trigger)
    try:
        provider_response = _post_json(
            _build_target_url(config.api_base, config.app_mode),
            _build_body(request, config, prompt),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            timeout_seconds=config.request_timeout_seconds,
        )
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"ai_auto_response request failed: {exc}")

    http_status = int(provider_response.get("http_status") or 200)
    if http_status >= 400:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message=str(provider_response.get("message") or provider_response.get("error") or "provider error"),
            output={"provider_response": provider_response},
        )

    answer = _extract_text(provider_response).strip()
    if not answer:
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="provider returned empty answer",
            output={"provider_status": "empty", "provider_response": provider_response, "trigger": trigger},
        )

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="ai auto response completed",
        output={
            "provider_status": "generated",
            "answer": answer,
            "history_size": len(history),
            "trigger": trigger,
            "provider_response": provider_response,
            "reply_text": answer,
            "replies": [{"type": "text", "content": answer}],
        },
    )


def _load_config(request: PluginExecutionRequest) -> AutoResponseConfig:
    raw = dict(request.config)
    return AutoResponseConfig(
        enabled=_as_bool(raw.get("enabled"), default=True),
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        app_mode=_resolve_app_mode(raw),
        workflow_input_key=str(raw.get("workflow_input_key") or "query").strip() or "query",
        workflow_session_key=str(raw.get("workflow_session_key") or "conversation_id").strip(),
        request_timeout_seconds=_as_int(raw.get("request_timeout_seconds"), default=DEFAULT_TIMEOUT_SECONDS, minimum=5),
        history_limit=_as_int(raw.get("history_limit"), default=8, minimum=1),
        trigger_direct_chat=_as_bool(raw.get("trigger_direct_chat"), default=True),
        trigger_group_mention=_as_bool(raw.get("trigger_group_mention"), default=True),
        trigger_group_probability=_as_float(raw.get("trigger_group_probability"), default=0.0, minimum=0.0, maximum=1.0),
        min_group_history=_as_int(raw.get("min_group_history"), default=3, minimum=1),
        system_prompt=str(
            raw.get("system_prompt")
            or "You are a concise and friendly chat assistant in a multi-user conversation. "
            "Reply naturally in Chinese, keep it short, and avoid repeating the recent chat."
        ).strip(),
    )


def _resolve_app_mode(config: dict[str, object]) -> str:
    app_mode = str(config.get("app_mode") or "").strip().lower()
    if app_mode in {"chat", "workflow"}:
        return app_mode
    return "chat"


def _resolve_trigger_state(request: PluginExecutionRequest, config: AutoResponseConfig) -> dict[str, object]:
    payload = dict(request.payload)
    normalized_event = payload.get("normalized_event")
    details = normalized_event.get("details") if isinstance(normalized_event, dict) else {}
    conversation = normalized_event.get("conversation") if isinstance(normalized_event, dict) else {}
    chat_type = str((conversation or {}).get("chat_type") or payload.get("chat_type") or "").strip().lower()
    mentioned_self = bool((details or {}).get("mentioned_self") or payload.get("mentioned_self"))
    command_name = str(payload.get("command_name") or "").strip().lower()

    if command_name in AUTO_RESPONSE_COMMANDS:
        return {"skip": False, "reason": "command_trigger", "chat_type": chat_type, "mentioned_self": mentioned_self}
    if chat_type == "direct":
        if config.trigger_direct_chat:
            return {"skip": False, "reason": "direct_chat", "chat_type": chat_type, "mentioned_self": mentioned_self}
        return {"skip": True, "reason": "direct_chat_disabled", "chat_type": chat_type, "mentioned_self": mentioned_self}
    if mentioned_self:
        if config.trigger_group_mention:
            return {"skip": False, "reason": "group_mention", "chat_type": chat_type, "mentioned_self": mentioned_self}
        return {"skip": True, "reason": "group_mention_disabled", "chat_type": chat_type, "mentioned_self": mentioned_self}

    history = _load_recent_history(request, limit=max(config.min_group_history, 1))
    if len(history) < config.min_group_history:
        return {"skip": True, "reason": "group_history_too_short", "history_size": len(history), "chat_type": chat_type}
    if config.trigger_group_probability <= 0:
        return {"skip": True, "reason": "group_auto_probability_disabled", "chat_type": chat_type}
    if random.random() > config.trigger_group_probability:
        return {"skip": True, "reason": "group_auto_probability_miss", "chat_type": chat_type}
    return {"skip": False, "reason": "group_auto_probability_hit", "chat_type": chat_type, "history_size": len(history)}


def _resolve_query(request: PluginExecutionRequest) -> str:
    payload = dict(request.payload)
    raw = str(
        payload.get("command_args")
        or payload.get("stripped_query")
        or payload.get("stripped_content")
        or payload.get("query")
        or payload.get("content")
        or ""
    )
    normalized = raw.replace("\u2005", " ").strip()
    normalized = re.sub(r"@\S+\s*", "", normalized)
    return normalized.strip()


def _load_recent_history(request: PluginExecutionRequest, *, limit: int) -> list[dict[str, str]]:
    conversation_id = _as_int(request.payload.get("conversation_id"), default=0, minimum=0)
    if conversation_id <= 0:
        return []
    with SessionLocal() as db:
        rows = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(limit)
        ).all()
    history: list[dict[str, str]] = []
    for row in reversed(rows):
        speaker = str(row.sender_name or row.sender_id or ("bot" if row.direction == "out" else "user")).strip()
        content = str(row.content or "").strip()
        if not content:
            continue
        history.append(
            {
                "speaker": speaker,
                "direction": str(row.direction or "").strip(),
                "content": content,
            }
        )
    return history


def _build_prompt(
    config: AutoResponseConfig,
    *,
    history: list[dict[str, str]],
    query: str,
    trigger: dict[str, object],
) -> str:
    lines = [config.system_prompt, "", f"Trigger: {trigger.get('reason') or 'unknown'}", ""]
    if history:
        lines.append("Recent conversation:")
        for item in history:
            lines.append(f"{item['speaker']}: {item['content']}")
        lines.append("")
    lines.append(f"Latest user message: {query}")
    lines.append("Reply with one short natural message.")
    return "\n".join(lines).strip()


def _build_target_url(api_base: str, app_mode: str) -> str:
    if app_mode == "workflow":
        return f"{api_base}/workflows/run"
    return f"{api_base}/chat-messages"


def _build_body(request: PluginExecutionRequest, config: AutoResponseConfig, prompt: str) -> dict[str, object]:
    payload = dict(request.payload)
    user = str(payload.get("user") or payload.get("sender_id") or "multi-bot-admin").strip() or "multi-bot-admin"
    if config.app_mode == "workflow":
        inputs = {
            config.workflow_input_key: prompt,
        }
        session_value = (
            payload.get("conversation_external_id")
            or payload.get("conversation_id")
            or payload.get("chat_id")
            or payload.get("room_id")
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
        "conversation_id": str(payload.get("conversation_id") or ""),
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
        outputs = data.get("outputs")
        if isinstance(outputs, dict):
            for key in ("answer", "text", "result", "content"):
                value = outputs.get(key)
                if value not in (None, ""):
                    return str(value)
            if len(outputs) == 1:
                only_value = next(iter(outputs.values()))
                if only_value not in (None, ""):
                    return str(only_value)
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


def _as_float(value: object, *, default: float, minimum: float, maximum: float) -> float:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, resolved))
