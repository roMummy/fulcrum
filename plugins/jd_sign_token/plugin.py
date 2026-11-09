from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.models.entities import Conversation, Message
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import standard_replies, text_reply


COMMAND = "设置京东"
DEFAULT_ENV_NAME = "JD_COOKIE"
TOKEN_PATTERN = re.compile(
    r"pt_key=([^;\s]+)\s*;\s*pt_pin=([^;\s]+)\s*;",
    re.IGNORECASE,
)
ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class QlConfig:
    """租户自己的青龙开放接口配置。"""

    ql_host: str
    client_id: str
    client_secret: str
    env_name: str
    timeout_seconds: int


@dataclass(frozen=True)
class ParsedSubmission:
    """从可信私聊消息中解析出的单个京东 Cookie。"""

    token: str
    pt_pin: str
    remark: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供配置检查，令牌写入必须来自真实私聊消息。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="京东签到令牌插件已就绪。",
            output={
                "ql_host_configured": bool(config.ql_host),
                "client_id_configured": bool(config.client_id),
                "client_secret_configured": bool(config.client_secret),
                "env_name": config.env_name,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("京东令牌只能通过真实私聊消息提交。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """把私聊提交的京东 Cookie 新增或更新到当前租户的青龙面板。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    if not config.ql_host or not config.client_id or not config.client_secret:
        return _error_result("请先配置当前租户的青龙地址和开放接口凭证。", "ql_not_configured")
    if not ENV_NAME_PATTERN.fullmatch(config.env_name):
        return _error_result("青龙环境变量名称格式不正确。", "invalid_env_name")

    _context, _conversation, message, error = _require_direct_message_context(db, request.context)
    if error is not None or message is None:
        return error or _error_result("京东令牌缺少可信私聊消息。", "trusted_context_required")
    try:
        submission = _parse_submission(message.content)
    except ValueError as exc:
        return _error_result(str(exc), "invalid_submission")

    try:
        client = PluginHttpClient()
        authorization = _fetch_access_token(client, config)
        envs = _fetch_envs(client, config, authorization)
        existing = _find_existing_env(envs, config.env_name, submission.pt_pin)
        if existing is None:
            _create_env(client, config, authorization, submission)
            provider_status = "created"
            action_text = "已添加"
        else:
            env_id = str(existing.get("id") or "").strip()
            if not env_id:
                raise ValueError("青龙环境变量缺少编号")
            _update_env(client, config, authorization, submission, env_id=env_id)
            provider_status = "updated"
            action_text = "已更新"
    except (PluginHttpError, ValueError, json.JSONDecodeError):
        # 上游异常不能把 URL、开放接口凭证、访问令牌或 Cookie 片段回显给用户。
        return _error_result("青龙服务暂时不可用，请检查配置后重试。", "ql_request_failed")

    account_hint = _mask_account(submission.pt_pin)
    reply_text = f"{action_text}京东账号“{submission.remark}”，账号标识：{account_hint}。"
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"{action_text}京东签到令牌。",
        output={
            "provider_status": provider_status,
            "env_name": config.env_name,
            "remark": submission.remark,
            "account_hint": account_hint,
            # 完整 Cookie 和 pt_pin 不进入插件执行日志，只发送业务确认文本。
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def _parse_submission(content: str) -> ParsedSubmission:
    """解析唯一命令格式，不接受 payload token、别名或旧字段。"""

    normalized = str(content or "").strip()
    if normalized[:1] in {"/", "#"}:
        normalized = normalized[1:].lstrip()
    if normalized == COMMAND:
        raise ValueError("命令格式：设置京东 pt_key=xxx;pt_pin=xxx; 备注")
    if not normalized.startswith(f"{COMMAND} "):
        raise ValueError("当前消息不是设置京东命令。")
    arguments = normalized[len(COMMAND) :].strip()
    match = TOKEN_PATTERN.search(arguments)
    if match is None or match.start() != 0:
        raise ValueError("Cookie 格式错误，应包含 pt_key 和 pt_pin。")
    pt_key = match.group(1).strip()
    pt_pin = match.group(2).strip()
    remark = _single_line(arguments[match.end() :].strip(" ;"), limit=80)
    if not remark:
        raise ValueError("请在 Cookie 后填写账号备注。")
    if len(pt_key) > 1024 or len(pt_pin) > 256:
        raise ValueError("Cookie 字段长度超过限制。")
    return ParsedSubmission(
        token=f"pt_key={pt_key};pt_pin={pt_pin};",
        pt_pin=pt_pin,
        remark=remark,
    )


def _fetch_access_token(client: PluginHttpClient, config: QlConfig) -> str:
    """登录青龙开放接口；查询参数会由统一 HTTP 日志自动脱敏。"""

    query = urlencode({"client_id": config.client_id, "client_secret": config.client_secret})
    payload = _request_json(
        client,
        "GET",
        f"{config.ql_host}/open/auth/token?{query}",
        config=config,
    )
    _require_ql_success(payload, fallback="青龙开放接口登录失败")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise ValueError("青龙登录响应缺少 data")
    token_type = str(data.get("token_type") or "Bearer").strip()
    token = str(data.get("token") or "").strip()
    if not token or not token_type:
        raise ValueError("青龙登录响应缺少访问令牌")
    return f"{token_type} {token}"


def _fetch_envs(
    client: PluginHttpClient,
    config: QlConfig,
    authorization: str,
) -> list[Mapping[str, Any]]:
    """只查询目标环境变量，避免下载并遍历租户青龙中的全部配置。"""

    payload = _request_json(
        client,
        "GET",
        f"{config.ql_host}/open/envs?{urlencode({'searchValue': config.env_name})}",
        config=config,
        headers={"Authorization": authorization},
    )
    _require_ql_success(payload, fallback="获取青龙环境变量失败")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("青龙环境变量响应不是列表")
    return [item for item in data if isinstance(item, Mapping)]


def _find_existing_env(
    envs: list[Mapping[str, Any]],
    env_name: str,
    pt_pin: str,
) -> Mapping[str, Any] | None:
    """按固定环境变量名和 Cookie 内的 pt_pin 定位现有账号。"""

    for item in envs:
        if str(item.get("name") or "").strip() != env_name:
            continue
        existing_pin = _extract_pt_pin(str(item.get("value") or ""))
        if existing_pin == pt_pin:
            return item
    return None


def _create_env(
    client: PluginHttpClient,
    config: QlConfig,
    authorization: str,
    submission: ParsedSubmission,
) -> None:
    payload = _request_json(
        client,
        "POST",
        f"{config.ql_host}/open/envs",
        config=config,
        headers={"Authorization": authorization},
        json_body=[
            {
                "name": config.env_name,
                "value": submission.token,
                "remarks": submission.remark,
            }
        ],
    )
    _require_ql_success(payload, fallback="新增青龙环境变量失败")


def _update_env(
    client: PluginHttpClient,
    config: QlConfig,
    authorization: str,
    submission: ParsedSubmission,
    *,
    env_id: str,
) -> None:
    payload = _request_json(
        client,
        "PUT",
        f"{config.ql_host}/open/envs",
        config=config,
        headers={"Authorization": authorization},
        json_body={
            "id": env_id,
            "name": config.env_name,
            "value": submission.token,
            "remarks": submission.remark,
        },
    )
    _require_ql_success(payload, fallback="更新青龙环境变量失败")


def _request_json(
    client: PluginHttpClient,
    method: str,
    url: str,
    *,
    config: QlConfig,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
) -> Mapping[str, Any]:
    """执行一次受控请求，并拒绝非 2xx 或非对象 JSON 响应。"""

    request_headers = {"Accept": "application/json", **dict(headers or {})}
    response = client.request(
        method,
        url,
        headers=request_headers,
        json_body=json_body,
        connect_timeout_seconds=min(8, config.timeout_seconds),
        total_timeout_seconds=config.timeout_seconds,
        max_response_bytes=2 * 1024 * 1024,
        max_redirects=2,
        retry_count=1 if method == "GET" else 0,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"青龙 HTTP 状态异常: {response.status_code}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("青龙响应不是 JSON 对象")
    return payload


def _require_ql_success(payload: Mapping[str, Any], *, fallback: str) -> None:
    if int(payload.get("code") or 0) != 200:
        raise ValueError(str(payload.get("message") or fallback))


def _extract_pt_pin(token: str) -> str:
    match = TOKEN_PATTERN.search(str(token or ""))
    return match.group(2).strip() if match else ""


def _require_direct_message_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[
    PluginExecutionContext | None,
    Conversation | None,
    Message | None,
    PluginExecutionResult | None,
]:
    """京东 Cookie 只能来自同一租户、机器人和渠道的真实私聊入站消息。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type != "direct"
    ):
        return None, None, None, _error_result("请私聊机器人提交京东 Cookie。", "direct_message_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(context.channel_id)
        or str(conversation.chat_type or "") != "direct"
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "") != "in"
        or not str(message.sender_id or "").strip()
        or (context.sender_id is not None and str(context.sender_id).strip() != str(message.sender_id).strip())
    ):
        return None, None, None, _error_result("当前私聊消息上下文无效。", "trusted_context_required")
    return context, conversation, message, None


def _load_config(raw_config: Mapping[str, Any]) -> QlConfig:
    raw = dict(raw_config)
    return QlConfig(
        ql_host=str(raw.get("ql_host") or "").strip().rstrip("/"),
        client_id=str(raw.get("client_id") or "").strip(),
        client_secret=str(raw.get("client_secret") or "").strip(),
        env_name=str(raw.get("env_name") or DEFAULT_ENV_NAME).strip() or DEFAULT_ENV_NAME,
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=15, minimum=3, maximum=60),
    )


def _mask_account(pt_pin: str) -> str:
    normalized = str(pt_pin or "").strip()
    if len(normalized) <= 4:
        return "*" * len(normalized)
    return f"{normalized[:2]}***{normalized[-3:]}"


def _single_line(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(resolved, maximum))


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


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
