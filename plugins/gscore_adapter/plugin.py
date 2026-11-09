from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Channel, Conversation, Message
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse
from app.services.plugin_runtime.replies import PluginReplyError, image_reply, standard_replies, text_reply


PLUGIN_CODE = "gscore_adapter"
COMMAND = "早柚"
MAX_QUERY_LENGTH = 8_000
MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_CONTENT_ITEMS = 32
MAX_NODE_DEPTH = 2
DATA_IMAGE_PATTERN = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,(.+)$", re.IGNORECASE | re.DOTALL)
MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]*\)")


@dataclass(frozen=True)
class GScoreConfig:
    """保存当前租户绑定唯一生效的 GScore HTTP 配置。"""

    api_url: str
    api_key: str
    request_timeout_seconds: int
    max_replies: int


@dataclass(frozen=True)
class TrustedGScoreMessage:
    """保存经过数据库交叉验证的插件绑定、渠道、会话和入站消息。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message


def matches_event(context) -> dict[str, object]:
    """只匹配私聊或群聊中的 GScore 业务查询命令。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "")
    config = _load_config(getattr(context, "binding_config", {}) or {})
    is_command, _query = _extract_query(content)
    matched = chat_type in {"direct", "group"} and message_type == "text" and is_command
    return {
        "matched": matched,
        "reason": "gscore_command_hit" if matched else "gscore_command_required",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行入口只提供健康状态，业务查询必须来自可信入站消息。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="GScore 适配器运行正常。",
            output={
                "provider_configured": bool(config.api_url and config.api_key),
                "request_timeout_seconds": config.request_timeout_seconds,
                "max_replies": config.max_replies,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在聊天中发送“早柚 查询内容”。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """把可信入站文本转成 GScore 请求，并把响应压缩为平台标准回复。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)

    trusted, context_error = _require_trusted_message(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _error_result("当前消息上下文无效。", "trusted_context_required")
    if not config.api_url or not config.api_key:
        return _error_result("请先配置当前租户自己的 GScore 服务地址和 API Key。", "not_configured")

    is_command, query = _extract_query(str(trusted.message.content or ""))
    if not is_command:
        return _error_result("请使用“早柚 查询内容”发起查询。", "command_invalid")
    if not query:
        return _error_result("请输入需要查询的游戏内容。", "query_required")
    if len(query) > MAX_QUERY_LENGTH:
        return _error_result("查询内容过长，请精简后重试。", "query_too_long")

    provider_payload = _build_provider_payload(trusted, query)
    try:
        response = PluginHttpClient().post_json(
            config.api_url,
            provider_payload,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {config.api_key}",
            },
            connect_timeout_seconds=min(5, config.request_timeout_seconds),
            total_timeout_seconds=config.request_timeout_seconds,
            max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
            max_redirects=2,
        )
    except PluginHttpError:
        # 统一 HTTP 层已经限制地址、超时、重定向和响应体。这里不把底层地址、
        # 鉴权信息或网络异常文本写入用户回复和长期执行摘要。
        return _error_result("GScore 服务暂时不可用，请稍后重试。", "request_failed")

    if response.status_code < 200 or response.status_code >= 300:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="GScore 服务返回错误，请检查租户配置和上游状态。",
            output={
                "provider_status": "http_error",
                "http_status": int(response.status_code),
                "replies": [],
            },
        )
    try:
        replies = _parse_provider_replies(response, maximum=config.max_replies)
    except (ValueError, TypeError, json.JSONDecodeError, PluginReplyError):
        return _error_result("GScore 服务返回了无法处理的消息。", "response_invalid")

    # 外部响应中的 target_id、bot_id、tenant_id、channel_id 等字段全部丢弃。
    # 标准回复由当前执行链路投递回原会话，外部服务没有改选租户或渠道的机会。
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"GScore 已返回 {len(replies)} 条结果。",
        output={
            "provider_status": "completed",
            "reply_count": len(replies),
            "replies": replies,
        },
    )


def _require_trusted_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedGScoreMessage | None, PluginExecutionResult | None]:
    """交叉验证租户、绑定、机器人、渠道、会话和消息的完整归属链。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type not in {"direct", "group"}
        or context.message_type != "text"
    ):
        return None, _error_result("请从真实聊天消息发起 GScore 查询。", "trusted_context_required")

    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
    conversation_external_id = str(conversation.external_id or "").strip() if conversation is not None else ""
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
        or str(channel.channel_type or "").strip() != str(context.channel_type or "").strip()
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or str(conversation.chat_type or "").strip() != str(context.chat_type or "").strip()
        or not conversation_external_id
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip() != "in"
        or str(message.message_type or "").strip() != "text"
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
    ):
        return None, _error_result("当前消息上下文无效。", "trusted_context_required")

    return (
        TrustedGScoreMessage(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            message=message,
        ),
        None,
    )


def _build_provider_payload(trusted: TrustedGScoreMessage, query: str) -> dict[str, Any]:
    """生成固定 GScore 消息契约，不把 payload 中的任何身份字段带到外部。"""

    bot_external_id = f"mbs-bot-{int(trusted.execution.bot_id)}"
    sender_id = str(trusted.message.sender_id or "").strip()
    sender_name = _single_line(trusted.message.sender_name or sender_id, limit=128) or sender_id
    chat_type = str(trusted.conversation.chat_type or "direct").strip()
    message_id = str(trusted.message.external_message_id or "").strip() or f"mbs-message-{int(trusted.message.id)}"
    return {
        "bot_id": bot_external_id,
        "sender": {
            "age": 0,
            "area": "",
            "card": sender_name,
            "level": "",
            "nickname": sender_name,
            "role": "member",
            "sex": "unknown",
            "title": "",
            "user_id": sender_id,
            "avater": "",
        },
        "bot_self_id": bot_external_id,
        "msg_id": message_id,
        "user_type": chat_type,
        "group_id": str(trusted.conversation.external_id) if chat_type == "group" else "",
        "user_id": sender_id,
        "user_pm": 6,
        "content": [{"type": "text", "data": query}],
    }


def _parse_provider_replies(response: PluginHttpResponse, *, maximum: int) -> list[dict[str, Any]]:
    """解析固定 content 数组，只接受文本、Markdown、base64 图片和有限节点。"""

    content_type = str(response.headers.get("content-type") or "").lower()
    if "json" not in content_type:
        raise ValueError("GScore 响应不是 JSON")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("GScore 响应必须是对象")
    content = payload.get("content")
    if not isinstance(content, list) or not content or len(content) > MAX_PROVIDER_CONTENT_ITEMS:
        raise ValueError("GScore content 数量不合法")

    bounded_maximum = max(1, min(int(maximum), 8))
    replies: list[dict[str, Any]] = []
    for item in _flatten_content_nodes(content, depth=0):
        if len(replies) >= bounded_maximum:
            break
        reply = _content_item_to_reply(item)
        if reply is not None:
            replies.append(reply)
    if not replies:
        raise ValueError("GScore 没有返回受支持的消息")
    return standard_replies(replies, max_count=bounded_maximum)


def _flatten_content_nodes(items: list[Any], *, depth: int) -> list[Mapping[str, Any]]:
    """展开 GScore 合并节点，同时限制嵌套深度和总处理数量。"""

    flattened: list[Mapping[str, Any]] = []
    for raw_item in items[:MAX_PROVIDER_CONTENT_ITEMS]:
        if not isinstance(raw_item, Mapping):
            continue
        item_type = str(raw_item.get("type") or "").strip().lower()
        if item_type == "node":
            children = raw_item.get("data")
            if depth < MAX_NODE_DEPTH and isinstance(children, list):
                flattened.extend(_flatten_content_nodes(children, depth=depth + 1))
            continue
        flattened.append(raw_item)
        if len(flattened) >= MAX_PROVIDER_CONTENT_ITEMS:
            break
    return flattened[:MAX_PROVIDER_CONTENT_ITEMS]


def _content_item_to_reply(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """把一个受支持的 GScore 内容节点转换为平台标准回复。"""

    item_type = str(item.get("type") or "").strip().lower()
    data = str(item.get("data") or "").strip()
    if item_type == "text" and data:
        return text_reply(data)
    if item_type == "markdown" and data:
        markdown_text = _markdown_to_text(data)
        return text_reply(markdown_text) if markdown_text else None
    if item_type in {"image", "b64", "url"} and data:
        # 外部响应不能指挥平台访问任意图片 URL。GScore 图片只接受 data URI、
        # base64:// 或原始 base64，在内存中校验后交给标准图片回复。
        image_base64 = _extract_image_base64(data)
        return image_reply(base64_data=image_base64) if image_base64 else None
    return None


def _extract_image_base64(value: str) -> str:
    """提取并严格校验 GScore 图片 base64，不接受远程 URL 或文件路径。"""

    normalized = str(value or "").strip()
    if normalized.lower().startswith("base64://"):
        normalized = normalized[len("base64://") :].strip()
    else:
        data_match = DATA_IMAGE_PATTERN.match(normalized)
        if data_match:
            normalized = data_match.group(1).strip()
    if normalized.lower().startswith(("http://", "https://", "file://")):
        return ""
    try:
        base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError, TypeError):
        return ""
    return normalized


def _markdown_to_text(markdown_text: str) -> str:
    """移除常见 Markdown 标记，保留适合各渠道发送的可读纯文本。"""

    text = str(markdown_text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = MARKDOWN_IMAGE_PATTERN.sub(lambda match: match.group(1), text)
    text = MARKDOWN_LINK_PATTERN.sub(lambda match: match.group(1), text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__)(.*?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_query(content: str) -> tuple[bool, str]:
    """从真实消息解析唯一业务命令，统一接受平台支持的斜杠和井号前缀。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    parts = normalized.split(maxsplit=1)
    if not parts or parts[0] != COMMAND:
        return False, ""
    return True, parts[1].strip() if len(parts) > 1 else ""


def _load_config(raw_config: Mapping[str, Any]) -> GScoreConfig:
    """读取当前绑定配置，并限制网络时限和单次回复数量。"""

    raw = dict(raw_config)
    return GScoreConfig(
        api_url=str(raw.get("api_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        request_timeout_seconds=_bounded_int(
            raw.get("request_timeout_seconds"),
            default=20,
            minimum=3,
            maximum=60,
        ),
        max_replies=_bounded_int(raw.get("max_replies"), default=6, minimum=1, maximum=8),
    )


def _single_line(value: Any, *, limit: int) -> str:
    """把外部显示名压缩为有限单行文本，避免污染 GScore 请求结构。"""

    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized[:limit]


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """返回最小业务错误，不暴露租户地址、密钥或外部响应内容。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
