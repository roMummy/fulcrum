from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

from app.core.settings import settings
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse
from app.services.plugin_runtime.replies import file_reply, image_reply, standard_replies, text_reply


CHAT_COMMAND = "聊天"
DEFAULT_TIMEOUT_SECONDS = 40
MAX_HISTORY_MESSAGES = 20
MAX_DIFY_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DIFY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REMOTE_IMAGE_BYTES = 16 * 1024 * 1024
SUPPORTED_IMAGE_SIGNATURES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
    b"II*\x00",
    b"MM\x00*",
)


class DifyInputError(ValueError):
    """标准消息无法转换为安全的 Dify 输入。"""


@dataclass(frozen=True)
class DifyConfig:
    """保存租户自己的 Dify 连接与有限运行参数。"""

    api_base: str
    api_key: str
    app_mode: str
    request_timeout_seconds: int
    max_history_messages: int


@dataclass(frozen=True)
class DifyInput:
    """插件向 Dify 发送的渠道无关输入。"""

    query: str
    history: list[str]
    quote_type: str
    files: list[dict[str, str]]


def matches_event(context) -> dict[str, object]:
    """来源最新规则只接受“聊天”命令，或群聊中明确 @ 机器人。"""

    content = str(getattr(context, "content", "") or "").strip()
    command_name = str(getattr(context, "command_name", "") or "").strip()
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    mentioned_self = bool(getattr(context, "mentioned_self", False))
    content_command = _extract_leading_command(content)
    command_hit = command_name == CHAT_COMMAND or content_command == CHAT_COMMAND
    mention_hit = chat_type == "group" and mentioned_self
    matched = command_hit or mention_hit
    return {
        "matched": matched,
        "reason": "chat_command" if command_hit else ("group_mention" if mention_hit else "dify_no_trigger"),
        "debug": {
            "command_name": command_name or content_command,
            "chat_type": chat_type,
            "mentioned_self": mentioned_self,
        },
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """执行一次租户 Dify 对话，并把结果收敛为平台标准回复。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="Dify 对话插件已就绪。",
            output={
                "provider_configured": bool(config.api_base and config.api_key),
                "app_mode": config.app_mode,
                "request_timeout_seconds": config.request_timeout_seconds,
                "quote_support": ["text", "standard_image_attachment"],
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if not _is_trusted_text_context(request.context):
        return _ignored_result("trusted_text_context_required")
    if not _request_has_supported_trigger(request):
        return _ignored_result("chat_command_or_group_mention_required")
    if not config.api_base or not config.api_key:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="请先配置当前租户自己的 Dify 服务地址和 API Key。",
            output={"provider_status": "not_configured", "replies": []},
        )

    client = PluginHttpClient()
    try:
        dify_input = _build_dify_input(request, config=config, client=client)
    except (DifyInputError, PluginHttpError):
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="引用内容暂时无法作为 Dify 输入，请确认附件已由平台完成下载。",
            output={"provider_status": "invalid_input", "replies": []},
        )
    if not dify_input.query:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="请在“聊天”命令或 @ 机器人后输入问题。",
            output={"provider_status": "empty_query", "replies": []},
        )

    try:
        response = client.post_json(
            _build_target_url(config),
            _build_request_body(request, config=config, dify_input=dify_input),
            headers={"Authorization": f"Bearer {config.api_key}"},
            connect_timeout_seconds=min(5, config.request_timeout_seconds),
            total_timeout_seconds=config.request_timeout_seconds,
            max_response_bytes=MAX_DIFY_RESPONSE_BYTES,
            max_redirects=2,
        )
    except PluginHttpError:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="Dify 服务暂时不可用，请稍后重试。",
            output={"provider_status": "request_failed", "replies": []},
        )

    if response.status_code >= 400:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="Dify 返回错误，请检查租户应用配置和上游服务状态。",
            output={
                "provider_status": "http_error",
                "http_status": int(response.status_code),
                "replies": [],
            },
        )

    try:
        provider_payload = response.json()
        answer = _extract_answer_text(provider_payload)
        replies, reply_text, media_type = _build_safe_replies(
            provider_payload,
            answer=answer,
            client=client,
            config=config,
        )
        token_usage = _extract_token_usage(provider_payload)
    except (TypeError, ValueError, json.JSONDecodeError, PluginHttpError):
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="Dify 未返回可识别的文本或媒体结果。",
            output={"provider_status": "invalid_response", "replies": []},
        )
    if not replies:
        return PluginExecutionResult(
            ok=False,
            status="error",
            message="Dify 本次没有生成可发送的结果。",
            output={"provider_status": "empty_response", "replies": []},
        )

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="Dify 对话已完成。",
        output={
            "provider_status": "completed",
            "app_mode": config.app_mode,
            "reply_text": reply_text,
            "media_type": media_type,
            "token_usage": token_usage,
            "quote_type": dify_input.quote_type,
            "input_file_count": len(dify_input.files),
            "replies": replies,
        },
    )


def _load_config(raw_config: Mapping[str, Any]) -> DifyConfig:
    """规范租户配置，不读取平台共享地址或密钥。"""

    raw = dict(raw_config)
    return DifyConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        app_mode=_resolve_app_mode(raw.get("app_mode")),
        request_timeout_seconds=_bounded_int(
            raw.get("request_timeout_seconds"),
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=5,
            maximum=120,
        ),
        max_history_messages=_bounded_int(
            raw.get("max_history_messages"),
            default=10,
            minimum=0,
            maximum=MAX_HISTORY_MESSAGES,
        ),
    )


def _is_trusted_text_context(context: PluginExecutionContext | None) -> bool:
    """消息类型和会话类型只能来自平台创建的可信上下文。"""

    if context is None:
        return False
    chat_type = str(context.chat_type or "").strip().lower()
    message_type = str(context.message_type or "").strip().lower()
    return chat_type in {"direct", "group"} and message_type == "text"


def _request_has_supported_trigger(request: PluginExecutionRequest) -> bool:
    content_command = _extract_leading_command(str(request.payload.get("content") or ""))
    command_name = str(request.payload.get("command_name") or "").strip()
    if command_name == CHAT_COMMAND or content_command == CHAT_COMMAND:
        return True
    context = request.context
    return bool(context and context.chat_type == "group" and _resolve_mentioned_self(request.payload))


def _build_dify_input(
    request: PluginExecutionRequest,
    *,
    config: DifyConfig,
    client: PluginHttpClient,
) -> DifyInput:
    """从标准消息、引用和附件中构造 Dify 输入，不调用任何渠道客户端。"""

    query = _normalize_query(request.payload)
    quote_payload = _extract_quote_payload(request.payload)
    quote_type = ""
    if quote_payload:
        ref_type = _coerce_int(quote_payload.get("ref_type"))
        if ref_type == 1:
            quoted_text = str(quote_payload.get("ref_content") or "").strip()
            if quoted_text:
                query = quoted_text
                quote_type = "text"
        elif ref_type == 3:
            quote_type = "image"
            title_query = _strip_chat_trigger(str(quote_payload.get("title") or ""))
            if title_query:
                query = title_query

    history = _resolve_history(request.payload, limit=config.max_history_messages)
    files = _build_input_files(request, config=config, client=client)
    if quote_type == "image" and not files:
        # 旧实现会从微信协议字段直接下载图片。当前架构只允许标准附件进入插件，
        # 因此协议适配器尚未生成附件时明确失败，不做微信专用隐式降级。
        raise DifyInputError("quoted image is not available as a standard attachment")
    return DifyInput(query=query, history=history, quote_type=quote_type, files=files)


def _normalize_query(payload: Mapping[str, Any]) -> str:
    raw = str(
        payload.get("command_args")
        or payload.get("stripped_query")
        or payload.get("stripped_content")
        or payload.get("query")
        or payload.get("content")
        or ""
    )
    normalized = raw.replace("\u2005", " ").strip()
    normalized = re.sub(r"^@\S+\s+", "", normalized)
    return _strip_chat_trigger(normalized)


def _strip_chat_trigger(content: str) -> str:
    normalized = str(content or "").strip()
    normalized = re.sub(r"^[\/#]", "", normalized).strip()
    if normalized == CHAT_COMMAND:
        return ""
    if normalized.startswith(f"{CHAT_COMMAND} "):
        return normalized[len(CHAT_COMMAND) :].strip()
    return normalized


def _extract_leading_command(content: str) -> str:
    normalized = str(content or "").strip()
    normalized = re.sub(r"^[\/#]", "", normalized).strip()
    return normalized.split(None, 1)[0] if normalized else ""


def _extract_quote_payload(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    direct = payload.get("quote_payload")
    if isinstance(direct, Mapping):
        return direct
    normalized_event = payload.get("normalized_event")
    details = normalized_event.get("details") if isinstance(normalized_event, Mapping) else None
    quote_payload = details.get("quote_payload") if isinstance(details, Mapping) else None
    return quote_payload if isinstance(quote_payload, Mapping) else None


def _resolve_mentioned_self(payload: Mapping[str, Any]) -> bool:
    normalized_event = payload.get("normalized_event")
    details = normalized_event.get("details") if isinstance(normalized_event, Mapping) else None
    if isinstance(details, Mapping) and "mentioned_self" in details:
        return bool(details.get("mentioned_self"))
    return bool(payload.get("mentioned_self"))


def _resolve_history(payload: Mapping[str, Any], *, limit: int) -> list[str]:
    if limit <= 0:
        return []
    candidates: list[Any] = [payload.get("messages")]
    normalized_event = payload.get("normalized_event")
    if isinstance(normalized_event, Mapping):
        candidates.extend(
            [
                normalized_event.get("messages"),
                normalized_event.get("recent_messages"),
                normalized_event.get("history_messages"),
            ]
        )
        details = normalized_event.get("details")
        if isinstance(details, Mapping):
            candidates.extend([details.get("recent_messages"), details.get("history_messages")])
    for candidate in candidates:
        normalized = _normalize_history_items(candidate)
        if normalized:
            return normalized[-limit:]
    return []


def _normalize_history_items(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            content = str(item.get("content") or item.get("text") or "").strip()
            sender = str(item.get("sender_name") or item.get("sender") or item.get("nickname") or "").strip()
            text = f"{sender}: {content}" if sender and content else content
        else:
            text = str(item or "").strip()
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            result.append(text[:500])
    return result


def _build_input_files(
    request: PluginExecutionRequest,
    *,
    config: DifyConfig,
    client: PluginHttpClient,
) -> list[dict[str, str]]:
    """把最多三张标准图片附件转换为 Dify remote_url 或 local_file 输入。"""

    attachments = _extract_standard_attachments(request.payload)
    if not attachments:
        return []
    user = str((request.context.sender_id if request.context else None) or "group-member")
    files: list[dict[str, str]] = []
    for attachment in attachments:
        attachment_type = str(attachment.get("type") or attachment.get("attachment_type") or "").lower()
        if attachment_type != "image":
            continue
        remote_url = _extract_attachment_url(attachment)
        if remote_url:
            files.append({"type": "image", "transfer_method": "remote_url", "url": remote_url})
        else:
            local_file = _read_owned_attachment(attachment)
            if local_file is None:
                continue
            file_name, content_type, file_bytes = local_file
            upload_id = _upload_dify_file(
                client,
                config=config,
                user=user,
                file_name=file_name,
                content_type=content_type,
                file_bytes=file_bytes,
            )
            files.append({"type": "image", "transfer_method": "local_file", "upload_file_id": upload_id})
        if len(files) >= 3:
            break
    return files


def _extract_standard_attachments(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Any] = [payload.get("attachments")]
    normalized_event = payload.get("normalized_event")
    if isinstance(normalized_event, Mapping):
        candidates.append(normalized_event.get("attachments"))
        details = normalized_event.get("details")
        if isinstance(details, Mapping):
            candidates.append(details.get("attachments"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, Mapping)]
    return []


def _extract_attachment_url(attachment: Mapping[str, Any]) -> str:
    candidates = [attachment]
    metadata = attachment.get("metadata")
    if isinstance(metadata, Mapping):
        candidates.append(metadata)
        details = metadata.get("details")
        if isinstance(details, Mapping):
            candidates.append(details)
    for candidate in candidates:
        for key in ("download_url", "file_url", "url"):
            value = str(candidate.get(key) or "").strip()
            parsed = urlsplit(value)
            if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None:
                return value
    return ""


def _read_owned_attachment(attachment: Mapping[str, Any]) -> tuple[str, str, bytes] | None:
    raw_path = str(attachment.get("storage_path") or attachment.get("local_path") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path).resolve()
    data_root = Path(settings.data_dir).resolve()
    try:
        path.relative_to(data_root)
    except ValueError:
        return None
    if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > MAX_DIFY_UPLOAD_BYTES:
        return None
    file_name = _safe_file_name(str(attachment.get("file_name") or path.name))
    content_type = _safe_content_type(
        str(attachment.get("mime_type") or mimetypes.guess_type(file_name)[0] or "image/jpeg")
    )
    return file_name, content_type, path.read_bytes()


def _upload_dify_file(
    client: PluginHttpClient,
    *,
    config: DifyConfig,
    user: str,
    file_name: str,
    content_type: str,
    file_bytes: bytes,
) -> str:
    """使用受控 HTTP 客户端上传标准附件，不调用渠道协议或第三方 SDK。"""

    boundary = f"----MultiBotSaaS{uuid.uuid4().hex}"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("ascii"))
    body.extend(b'Content-Disposition: form-data; name="user"\r\n\r\n')
    body.extend(user.encode("utf-8"))
    body.extend(f"\r\n--{boundary}\r\n".encode("ascii"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(file_bytes)
    body.extend(f"\r\n--{boundary}--\r\n".encode("ascii"))
    response = client.request(
        "POST",
        f"{config.api_base}/files/upload",
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        body=bytes(body),
        connect_timeout_seconds=min(5, config.request_timeout_seconds),
        total_timeout_seconds=config.request_timeout_seconds,
        max_response_bytes=256 * 1024,
        max_redirects=2,
    )
    if response.status_code >= 400:
        raise DifyInputError("Dify file upload failed")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise DifyInputError("Dify file upload payload is invalid")
    upload_id = str(payload.get("id") or "").strip()
    data = payload.get("data")
    if not upload_id and isinstance(data, Mapping):
        upload_id = str(data.get("id") or data.get("file_id") or "").strip()
    if not upload_id:
        raise DifyInputError("Dify file upload id is missing")
    return upload_id


def _build_target_url(config: DifyConfig) -> str:
    return f"{config.api_base}/workflows/run" if config.app_mode == "workflow" else f"{config.api_base}/chat-messages"


def _build_request_body(
    request: PluginExecutionRequest,
    *,
    config: DifyConfig,
    dify_input: DifyInput,
) -> dict[str, object]:
    context = request.context
    user = str((context.sender_id if context else None) or "group-member").strip() or "group-member"
    session_id = str(
        request.payload.get("conversation_external_id")
        or (context.conversation_id if context else None)
        or ""
    ).strip()
    if config.app_mode == "workflow":
        inputs: dict[str, object] = {"query": dify_input.query, "conversation_id": session_id}
        if dify_input.history:
            inputs["history"] = "\n".join(dify_input.history)
        return {
            "inputs": inputs,
            "files": dify_input.files,
            "response_mode": "blocking",
            "user": user,
        }
    return {
        "query": dify_input.query,
        "inputs": {"history": "\n".join(dify_input.history)} if dify_input.history else {},
        "files": dify_input.files,
        # 平台尚未持久化 Dify 自身返回的 conversation_id，因此不能把本地会话编号
        # 冒充为 Dify 会话编号。上下文通过标准 history 输入显式提供。
        "conversation_id": "",
        "response_mode": "blocking",
        "user": user,
    }


def _build_safe_replies(
    provider_payload: Any,
    *,
    answer: str,
    client: PluginHttpClient,
    config: DifyConfig,
) -> tuple[list[dict[str, Any]], str, str]:
    """把文本、内联图片和媒体 URL 转换为统一回复，丢弃其余上游字段。"""

    replies: list[dict[str, Any]] = []
    reply_text = ""
    media_type = ""
    inline_image = _extract_inline_image_base64(answer)
    if inline_image:
        replies.append(image_reply(base64_data=inline_image))
        media_type = "image"
    else:
        descriptor = _extract_media_descriptor(provider_payload)
        if descriptor:
            media_type = descriptor[0]
            media_reply = _build_media_reply(
                descriptor[0],
                descriptor[1],
                client=client,
                timeout_seconds=config.request_timeout_seconds,
            )
            if media_reply:
                replies.append(media_reply)

        cleaned_answer = _sanitize_answer(answer)
        if cleaned_answer and not _looks_like_media_url(cleaned_answer, media_type):
            reply_text = cleaned_answer
            replies.append(text_reply(cleaned_answer))
    return standard_replies(replies), reply_text, media_type


def _extract_answer_text(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError("Dify response must be an object")
    for key in ("answer", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    data = payload.get("data")
    if isinstance(data, Mapping):
        for key in ("answer", "text", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        outputs = data.get("outputs")
        if isinstance(outputs, Mapping):
            for key in ("text", "answer", "content"):
                value = outputs.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            output_type = str(outputs.get("type") or "").strip().lower()
            result = outputs.get("result")
            if output_type not in {"image", "video"} and isinstance(result, str) and result.strip():
                return result.strip()
    return ""


def _extract_media_descriptor(payload: Any) -> tuple[str, str] | None:
    if not isinstance(payload, Mapping):
        return None
    candidates: list[Any] = [payload.get("outputs"), payload.get("output")]
    data = payload.get("data")
    if isinstance(data, Mapping):
        candidates.extend([data.get("outputs"), data.get("output")])
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        media_type = str(candidate.get("type") or "").strip().lower()
        media_url = str(candidate.get("result") or candidate.get("url") or "").strip()
        if media_type in {"image", "video"} and media_url:
            return media_type, media_url
    return None


def _build_media_reply(
    media_type: str,
    media_url: str,
    *,
    client: PluginHttpClient,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    parsed = urlsplit(media_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        return None
    if media_type == "video":
        # 当前标准视频回复需要独立封面。Dify 只返回视频 URL 时以通用文件交付，
        # 避免在插件中重新引入 ffmpeg 和渠道专用视频发送逻辑。
        file_name = _safe_file_name(unquote(Path(parsed.path).name) or "dify-video.mp4")
        return file_reply(file_name=file_name, file_url=media_url)
    response = client.request(
        "GET",
        media_url,
        connect_timeout_seconds=min(5, timeout_seconds),
        total_timeout_seconds=timeout_seconds,
        max_response_bytes=MAX_REMOTE_IMAGE_BYTES,
        max_redirects=2,
        retry_count=1,
    )
    if response.status_code >= 400 or not _is_supported_image(response.body):
        return None
    return image_reply(base64_data=base64.b64encode(response.body).decode("ascii"))


def _extract_inline_image_base64(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    markdown_match = re.search(
        r"!\[[^\]]*\]\((data:image/[^)]+)\)",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if markdown_match:
        normalized = markdown_match.group(1).strip()
    json_candidate = _extract_image_candidate_from_json(normalized)
    if json_candidate:
        normalized = json_candidate
        # JSON 字段有时继续包着 Markdown 图片语法。提取结构化字段后再识别一次，
        # 避免把 `![image](data:...)` 当成普通 base64 而丢失图片结果。
        nested_markdown = re.search(
            r"!\[[^\]]*\]\((data:image/[^)]+)\)",
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if nested_markdown:
            normalized = nested_markdown.group(1).strip()
    if normalized.lower().startswith("data:image/"):
        _, separator, encoded = normalized.partition(",")
        if not separator or ";base64" not in normalized[: normalized.find(",")].lower():
            return ""
        normalized = encoded.strip()
    compact = re.sub(r"\s+", "", normalized)
    if len(compact) < 16 or not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        return ""
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        return ""
    return compact if _is_supported_image(decoded) else ""


def _extract_image_candidate_from_json(value: str) -> str:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    for key in ("b64_json", "image_base64", "base64", "image_data", "data"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for key in ("text", "answer", "result", "content"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and "data:image/" in candidate:
            return candidate.strip()
    return ""


def _is_supported_image(content: bytes) -> bool:
    if len(content) < 12:
        return False
    if content.startswith(SUPPORTED_IMAGE_SIGNATURES):
        return True
    return content.startswith(b"RIFF") and content[8:12] == b"WEBP"


def _sanitize_answer(value: str) -> str:
    normalized = re.sub(r"<think\b[^>]*>.*?</think>", "", str(value or ""), flags=re.IGNORECASE | re.DOTALL)
    normalized = re.sub(r"<think\b[^>]*>.*$", "", normalized, flags=re.IGNORECASE | re.DOTALL)
    normalized = re.sub(r"</?grok:render[^>]*>", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return normalized[:20_000]


def _looks_like_media_url(answer: str, media_type: str) -> bool:
    if not media_type:
        return False
    parsed = urlsplit(answer)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _extract_token_usage(payload: Any) -> int:
    if not isinstance(payload, Mapping):
        return 0
    candidates: list[Any] = [payload.get("usage"), payload.get("metadata")]
    data = payload.get("data")
    if isinstance(data, Mapping):
        if data.get("total_tokens") not in (None, ""):
            return max(0, _coerce_int(data.get("total_tokens")))
        candidates.extend([data.get("usage"), data.get("metadata")])
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            total = candidate.get("total_tokens")
            if total not in (None, ""):
                return max(0, _coerce_int(total))
            usage = candidate.get("usage")
            if isinstance(usage, Mapping) and usage.get("total_tokens") not in (None, ""):
                return max(0, _coerce_int(usage.get("total_tokens")))
    return 0


def _safe_file_name(value: str) -> str:
    normalized = Path(str(value or "")).name.strip()
    normalized = normalized.replace('"', "").replace("\r", "").replace("\n", "")
    return normalized[:255] or "dify-file"


def _safe_content_type(value: str) -> str:
    """限制 multipart MIME 头字符，阻止附件元数据注入额外请求头。"""

    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*", normalized):
        return normalized
    return "application/octet-stream"


def _ignored_result(reason: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="ignored",
        message="本次消息不进入 Dify 对话。",
        output={"provider_status": "ignored", "reason": reason, "replies": []},
    )


def _resolve_app_mode(value: object) -> str:
    normalized = str(value or "workflow").strip().lower()
    return normalized if normalized in {"chat", "workflow"} else "workflow"


def _as_bool(value: object, *, default: bool) -> bool:
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


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return min(maximum, max(minimum, normalized))


def _coerce_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
