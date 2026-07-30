from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib import parse as urlparse
from urllib import request as urlrequest

from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_ENV_NAME = "JD_COOKIE"
TOKEN_PATTERN = re.compile(r"pt_key=([^;]+);?\s*pt_pin=([^;]+);?", re.IGNORECASE)


@dataclass(frozen=True)
class QlConfig:
    enabled: bool
    host: str
    client_id: str
    client_secret: str
    env_name: str
    commands: tuple[str, ...]


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="jd_sign_token runtime is ready",
            output={
                "enabled": config.enabled,
                "host": config.host,
                "env_name": config.env_name,
                "commands": list(config.commands),
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not config.enabled:
        return PluginExecutionResult(ok=False, status="disabled", message="京东签到令牌插件当前未启用。")
    if not config.host or not config.client_id or not config.client_secret:
        return PluginExecutionResult(ok=False, status="error", message="缺少青龙面板配置，请补充 ql_host / client_id / client_secret。")

    parsed = _parse_submission(request.payload)
    if not parsed.ok:
        return PluginExecutionResult(ok=False, status="error", message=parsed.message)

    try:
        token = _fetch_access_token(config)
        envs = _fetch_envs(config, token)
        existing = _find_existing_env(envs, config.env_name, parsed.pt_pin)
        if existing:
            env_id = existing.get("id") or existing.get("_id")
            if not env_id:
                return PluginExecutionResult(ok=False, status="error", message="找到已有 JD_COOKIE，但缺少环境变量 ID，无法更新。")
            success = _update_env(
                config,
                token,
                {
                    "id": env_id,
                    "name": config.env_name,
                    "value": parsed.token,
                    "remarks": parsed.remark,
                },
            )
            if not success:
                return PluginExecutionResult(ok=False, status="error", message="更新青龙环境变量失败。")
            action = "updated"
            message = f"已更新京东账号 [{parsed.remark}] 的 Token。"
        else:
            success = _create_env(
                config,
                token,
                [{"name": config.env_name, "value": parsed.token, "remarks": parsed.remark}],
            )
            if not success:
                return PluginExecutionResult(ok=False, status="error", message="新增青龙环境变量失败。")
            action = "created"
            message = f"已添加京东账号 [{parsed.remark}] 的 Token。"
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"提交京东 Token 失败: {exc}")

    reply_text = f"{message}\npt_pin: {parsed.pt_pin}"
    replies: list[dict[str, str]] = [{"type": "text", "content": reply_text}]
    qrcode_path = Path(__file__).with_name("qrcode.png")
    if qrcode_path.exists():
        replies.append({"type": "image", "image_path": str(qrcode_path)})

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": action,
            "env_name": config.env_name,
            "remark": parsed.remark,
            "pt_pin": parsed.pt_pin,
            "reply_text": reply_text,
            "replies": replies,
        },
    )


@dataclass(frozen=True)
class ParsedSubmission:
    ok: bool
    message: str
    token: str = ""
    pt_pin: str = ""
    remark: str = ""


def _parse_submission(payload: dict[str, object]) -> ParsedSubmission:
    token = str(payload.get("token") or payload.get("jd_cookie") or "").strip()
    remark = str(payload.get("remark") or payload.get("title") or "").strip()
    if token:
        normalized = _normalize_token(token)
        if not normalized:
            return ParsedSubmission(ok=False, message="Token 格式错误，应包含 pt_key 和 pt_pin。")
        pt_pin = _extract_pt_pin(normalized)
        if not pt_pin:
            return ParsedSubmission(ok=False, message="无法从 Token 中提取 pt_pin。")
        final_remark = remark or pt_pin
        return ParsedSubmission(ok=True, message="ok", token=normalized, pt_pin=pt_pin, remark=final_remark)

    content = str(payload.get("content") or "").strip()
    command_name = str(payload.get("command_name") or "").strip()
    command_args = str(payload.get("command_args") or payload.get("stripped_content") or "").strip()
    raw = command_args or content
    if command_name and raw.startswith(command_name):
        raw = raw[len(command_name):].strip()
    match = TOKEN_PATTERN.search(raw)
    if not match:
        return ParsedSubmission(ok=False, message="命令格式错误，请使用：设置京东 pt_key=xxx;pt_pin=xxx; 备注")
    pt_key = match.group(1).strip()
    pt_pin = match.group(2).strip()
    normalized = f"pt_key={pt_key};pt_pin={pt_pin};"
    tail = raw[match.end():].strip(" ;")
    final_remark = remark or tail or pt_pin
    return ParsedSubmission(ok=True, message="ok", token=normalized, pt_pin=pt_pin, remark=final_remark)


def _normalize_token(value: str) -> str:
    match = TOKEN_PATTERN.search(value.replace("\n", " ").replace("\r", " "))
    if not match:
        return ""
    return f"pt_key={match.group(1).strip()};pt_pin={match.group(2).strip()};"


def _extract_pt_pin(token: str) -> str:
    match = re.search(r"pt_pin=([^;]+)", token, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _load_config(request: PluginExecutionRequest) -> QlConfig:
    return QlConfig(
        enabled=bool(request.config.get("enable", True)),
        host=str(request.config.get("ql_host") or request.config.get("QL_HOST") or "").strip().rstrip("/"),
        client_id=str(request.config.get("client_id") or request.config.get("CLIENT_ID") or "").strip(),
        client_secret=str(request.config.get("client_secret") or request.config.get("CLIENT_SECRET") or "").strip(),
        env_name=str(request.config.get("env_name") or DEFAULT_ENV_NAME).strip() or DEFAULT_ENV_NAME,
        commands=_normalize_commands(request.config.get("command"), ("设置京东", "jd_sign_token", "jd_token")),
    )


def _normalize_commands(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list):
        normalized = tuple(str(item).strip() for item in value if str(item).strip())
        return normalized or default
    if isinstance(value, str):
        normalized = tuple(item.strip() for item in value.split(",") if item.strip())
        return normalized or default
    return default


def _fetch_access_token(config: QlConfig) -> str:
    target = (
        f"{config.host}/open/auth/token?"
        f"client_id={urlparse.quote(config.client_id)}&client_secret={urlparse.quote(config.client_secret)}"
    )
    payload = _request_json(target, headers={"accept": "application/json"})
    if int(payload.get("code") or 0) != 200:
        raise ValueError(str(payload.get("message") or "青龙登录失败"))
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        raise ValueError("青龙登录返回无效")
    token = f"{data.get('token_type') or 'Bearer'} {data.get('token') or ''}".strip()
    if token.endswith(" "):
        raise ValueError("青龙访问令牌为空")
    return token


def _fetch_envs(config: QlConfig, token: str) -> list[dict[str, object]]:
    payload = _request_json(
        f"{config.host}/open/envs?searchValue=",
        headers={"Authorization": token, "accept": "application/json"},
    )
    if int(payload.get("code") or 0) != 200:
        raise ValueError(str(payload.get("message") or "获取环境变量失败"))
    data = payload.get("data") or []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _find_existing_env(envs: list[dict[str, object]], env_name: str, pt_pin: str) -> dict[str, object] | None:
    for item in envs:
        if str(item.get("name") or "").strip() != env_name:
            continue
        token = _normalize_token(str(item.get("value") or ""))
        if _extract_pt_pin(token) == pt_pin:
            return item
    return None


def _update_env(config: QlConfig, token: str, payload: dict[str, object]) -> bool:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = _request_json(
        f"{config.host}/open/envs",
        method="PUT",
        data=data,
        headers={
            "Authorization": token,
            "accept": "application/json",
            "content-type": "application/json",
        },
    )
    return int(result.get("code") or 0) == 200


def _create_env(config: QlConfig, token: str, payload: list[dict[str, object]]) -> bool:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = _request_json(
        f"{config.host}/open/envs",
        method="POST",
        data=data,
        headers={
            "Authorization": token,
            "accept": "application/json",
            "content-type": "application/json",
        },
    )
    return int(result.get("code") or 0) == 200


def _request_json(url: str, *, method: str = "GET", data: bytes | None = None, headers: dict[str, str] | None = None) -> dict[str, object]:
    req = urlrequest.Request(url, method=method, data=data, headers=headers or {})
    with urlrequest.urlopen(req, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")
