from __future__ import annotations

import base64
import binascii
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.entities import BotPlugin, Channel, Conversation, Message, MessageAttachment
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply


PLUGIN_CODE = "ai_gen_image"
COMMANDS = ("AI绘图", "绘图", "画图", "生成图片")
GENERATION_PATH = "/images/generations"
EDIT_PATH = "/images/edits"
MAX_PROMPT_LENGTH = 4_000
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PROVIDER_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_DATA_ITEMS = 4
SUPPORTED_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
SUPPORTED_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
)


@dataclass(frozen=True)
class ImageGenerationConfig:
    """保存当前租户绑定唯一生效的图片模型配置。"""

    api_base: str
    api_key: str
    model: str
    request_timeout_seconds: int


@dataclass(frozen=True)
class TrustedImageMessage:
    """保存经过数据库交叉验证的绘图命令及其完整归属链。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message


@dataclass(frozen=True)
class ReferenceImage:
    """保存当前消息可信图片附件的有限上传信息。"""

    file_name: str
    mime_type: str
    content: bytes


def matches_event(context) -> dict[str, object]:
    """只匹配私聊或群聊中的四个图片生成命令。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "")
    config = _load_config(getattr(context, "binding_config", {}) or {})
    is_command, _prompt = _extract_prompt(content)
    matched = chat_type in {"direct", "group"} and message_type == "text" and is_command
    return {
        "matched": matched,
        "reason": "image_command_hit" if matched else "image_command_required",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行入口只提供健康状态，生图必须由可信聊天消息触发。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="AI 绘图插件运行正常。",
            output={
                "provider_configured": bool(config.api_base and config.api_key and config.model),
                "model": config.model,
                "request_timeout_seconds": config.request_timeout_seconds,
                "supports_reference_image": True,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在聊天中发送“画图 图片描述”。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在异步消息任务中执行一次文本生图或单图编辑请求。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)

    trusted, context_error = _require_trusted_message(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _error_result("当前消息上下文无效。", "trusted_context_required")
    if not config.api_base or not config.api_key or not config.model:
        return _error_result("请先配置当前租户自己的图片模型地址、模型和 API Key。", "not_configured")

    is_command, prompt = _extract_prompt(str(trusted.message.content or ""))
    if not is_command:
        return _error_result("请使用“AI绘图、绘图、画图或生成图片 + 描述”发起请求。", "command_invalid")
    if not prompt:
        return _error_result("请输入需要生成的图片描述。", "prompt_required")
    if len(prompt) > MAX_PROMPT_LENGTH:
        return _error_result("图片描述过长，请精简后重试。", "prompt_too_long")

    reference_image = _load_reference_image(db, trusted)
    client = PluginHttpClient()
    try:
        if reference_image is None:
            mode = "generation"
            response = _request_generation(client, config=config, prompt=prompt)
        else:
            mode = "edit"
            response = _request_edit(
                client,
                config=config,
                prompt=prompt,
                reference_image=reference_image,
            )
    except PluginHttpError:
        # 网络层已经限制地址、超时、重定向和响应体。业务层不返回底层异常文本，
        # 防止租户地址、鉴权细节和供应商内部信息进入消息或执行摘要。
        return _error_result("图片模型暂时不可用，请稍后重试。", "request_failed")

    if response.status_code < 200 or response.status_code >= 300:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="图片模型返回错误，请检查租户配置和上游状态。",
            output={
                "provider_status": "http_error",
                "http_status": int(response.status_code),
                "replies": [],
            },
        )
    try:
        image_base64 = _extract_provider_image(response)
    except (ValueError, TypeError, json.JSONDecodeError):
        return _error_result("图片模型没有返回可用图片。", "response_invalid")

    # 只把最终图片放入标准回复。供应商 trace、revised_prompt、计费明细和完整
    # 响应不会进入输出、任务载荷或渠道消息，避免跨租户日志泄露和结果膨胀。
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="图片已生成。" if mode == "generation" else "图片已编辑。",
        output={
            "provider_status": "completed",
            "mode": mode,
            "replies": standard_replies([image_reply(base64_data=image_base64)]),
        },
    )


def _require_trusted_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedImageMessage | None, PluginExecutionResult | None]:
    """交叉验证租户、绑定、机器人、渠道、会话和入站消息的完整归属链。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type not in {"direct", "group"}
        or context.message_type != "text"
    ):
        return None, _error_result("请从真实聊天消息发起图片生成。", "trusted_context_required")

    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
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
        or not str(conversation.external_id or "").strip()
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
        TrustedImageMessage(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            message=message,
        ),
        None,
    )


def _load_reference_image(db: Session, trusted: TrustedImageMessage) -> ReferenceImage | None:
    """读取当前消息第一张已下载图片，拒绝 payload 路径和跨消息附件。"""

    attachment = db.scalar(
        select(MessageAttachment)
        .where(
            MessageAttachment.message_id == int(trusted.message.id),
            MessageAttachment.channel_id == int(trusted.channel.id),
            MessageAttachment.conversation_id == int(trusted.conversation.id),
            MessageAttachment.direction == "in",
            MessageAttachment.attachment_type == "image",
            MessageAttachment.status == "downloaded",
        )
        .order_by(MessageAttachment.id.asc())
        .limit(1)
    )
    if attachment is None or int(attachment.tenant_id) != int(trusted.execution.tenant_id):
        return None

    raw_path = str(attachment.storage_path or "").strip()
    if not raw_path:
        return None
    try:
        data_root = Path(settings.data_dir).expanduser().resolve(strict=True)
        image_path = Path(raw_path).expanduser().resolve(strict=True)
        image_path.relative_to(data_root)
    except (OSError, ValueError):
        return None
    if not image_path.is_file():
        return None

    file_size = image_path.stat().st_size
    if file_size <= 0 or file_size > MAX_REFERENCE_IMAGE_BYTES:
        return None
    content = image_path.read_bytes()
    if len(content) != file_size or not _is_supported_image(content):
        return None
    mime_type = str(attachment.mime_type or "").strip().lower()
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        mime_type = _detect_image_mime_type(content)
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        return None
    return ReferenceImage(
        file_name=_safe_file_name(image_path.name, mime_type=mime_type),
        mime_type=mime_type,
        content=content,
    )


def _request_generation(
    client: PluginHttpClient,
    *,
    config: ImageGenerationConfig,
    prompt: str,
) -> PluginHttpResponse:
    """调用固定 OpenAI 兼容文本生图接口，不转发载荷中的任意参数。"""

    return client.post_json(
        f"{config.api_base}{GENERATION_PATH}",
        {
            "model": config.model,
            "prompt": prompt,
            "response_format": "b64_json",
        },
        headers=_provider_headers(config.api_key),
        connect_timeout_seconds=min(8, config.request_timeout_seconds),
        total_timeout_seconds=config.request_timeout_seconds,
        max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        max_redirects=2,
    )


def _request_edit(
    client: PluginHttpClient,
    *,
    config: ImageGenerationConfig,
    prompt: str,
    reference_image: ReferenceImage,
) -> PluginHttpResponse:
    """调用固定 OpenAI 兼容图片编辑接口，并手工构造有限 multipart 请求。"""

    boundary = f"----MultiBotSaaS{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in (
        ("model", config.model),
        ("prompt", prompt),
        ("response_format", "b64_json"),
    ):
        _append_multipart_text(body, boundary=boundary, name=name, value=value)
    body.extend(f"--{boundary}\r\n".encode("ascii"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="image"; filename="{reference_image.file_name}"\r\n'
            f"Content-Type: {reference_image.mime_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(reference_image.content)
    body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
    return client.request(
        "POST",
        f"{config.api_base}{EDIT_PATH}",
        headers={
            **_provider_headers(config.api_key),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        body=bytes(body),
        connect_timeout_seconds=min(8, config.request_timeout_seconds),
        total_timeout_seconds=config.request_timeout_seconds,
        max_response_bytes=MAX_PROVIDER_RESPONSE_BYTES,
        max_redirects=2,
    )


def _append_multipart_text(body: bytearray, *, boundary: str, name: str, value: str) -> None:
    """向 multipart 请求追加一个固定名称的 UTF-8 文本字段。"""

    body.extend(f"--{boundary}\r\n".encode("ascii"))
    body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
    body.extend(str(value).encode("utf-8"))
    body.extend(b"\r\n")


def _extract_provider_image(response: PluginHttpResponse) -> str:
    """从固定 OpenAI data[].b64_json 契约提取第一张有效图片。"""

    content_type = str(response.headers.get("content-type") or "").lower()
    if "json" not in content_type:
        raise ValueError("图片模型响应不是 JSON")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise ValueError("图片模型响应必须是对象")
    data = payload.get("data")
    if not isinstance(data, list) or not data or len(data) > MAX_PROVIDER_DATA_ITEMS:
        raise ValueError("图片模型 data 数量不合法")

    for item in data:
        if not isinstance(item, Mapping):
            continue
        encoded = re.sub(r"\s+", "", str(item.get("b64_json") or ""))
        if not encoded:
            continue
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error):
            continue
        if decoded and _is_supported_image(decoded):
            return encoded
    raise ValueError("图片模型未返回有效图片 base64")


def _extract_prompt(content: str) -> tuple[bool, str]:
    """从真实入站文本解析命令和图片描述，统一支持斜杠与井号前缀。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    parts = normalized.split(maxsplit=1)
    if not parts or parts[0] not in COMMANDS:
        return False, ""
    return True, parts[1].strip() if len(parts) > 1 else ""


def _provider_headers(api_key: str) -> dict[str, str]:
    """生成固定供应商请求头，密钥仅在本次内存请求中使用。"""

    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _is_supported_image(content: bytes) -> bool:
    if any(content.startswith(signature) for signature in SUPPORTED_IMAGE_SIGNATURES):
        return True
    return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"


def _detect_image_mime_type(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _safe_file_name(raw_name: str, *, mime_type: str) -> str:
    """生成不含路径、引号和换行的有限 multipart 文件名。"""

    normalized = Path(str(raw_name or "")).name
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(".-")[:120]
    extension = {"image/png": ".png", "image/webp": ".webp"}.get(mime_type, ".jpg")
    if not normalized:
        return f"reference{extension}"
    if Path(normalized).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        normalized += extension
    return normalized


def _load_config(raw_config: Mapping[str, Any]) -> ImageGenerationConfig:
    """读取当前绑定配置，并限制模型名与网络超时。"""

    raw = dict(raw_config)
    return ImageGenerationConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        model=_single_line(raw.get("model"), limit=160),
        request_timeout_seconds=_bounded_int(
            raw.get("request_timeout_seconds"),
            default=90,
            minimum=10,
            maximum=120,
        ),
    )


def _single_line(value: Any, *, limit: int) -> str:
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
    """返回最小业务错误，不暴露模型地址、密钥或供应商响应。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
