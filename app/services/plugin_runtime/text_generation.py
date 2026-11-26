from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse


class PluginTextGenerationError(RuntimeError):
    """通用文本生成服务无法完成请求或返回了无效结果。"""


@dataclass(frozen=True)
class PluginTextGenerationResult:
    """插件可以持久化的最小生成结果，不包含供应商原始响应。"""

    text: str
    token_usage: int


@dataclass(frozen=True)
class GenerationImage:
    """已校验的有限图片字节，不允许模型服务代取任意 URL 或本地路径。"""

    data: bytes = field(repr=False)
    mime_type: str = "image/jpeg"
    caption: str = ""


class PluginTextGenerationClient:
    """基于 OpenAI-compatible 契约提供与具体供应商无关的文本生成能力。"""

    def __init__(
        self,
        *,
        api_base: str,
        model: str,
        api_key: str = "",
        http_client: PluginHttpClient | None = None,
    ):
        self.api_base = str(api_base or "").strip().rstrip("/")
        self.model = str(model or "").strip()
        self.api_key = str(api_key or "").strip()
        self.http_client = http_client or PluginHttpClient()
        if not self.api_base or not self.model:
            raise PluginTextGenerationError("文本生成服务需要配置 API 地址和模型")

    def generate(
        self,
        *,
        user_prompt: str,
        system_prompt: str = "",
        user_id: str = "plugin-user",
        timeout_seconds: int = 60,
        temperature: float = 0.3,
        max_tokens: int = 1600,
        images: tuple[GenerationImage, ...] = (),
        api_protocol: str = "chat_completions",
        reasoning_effort: str | None = None,
        stream: bool = False,
    ) -> PluginTextGenerationResult:
        """调用 OpenAI-compatible 文本接口，并只返回文本与总 token 数。

        ``stream`` 只改变上游传输方式：SSE 增量在客户端内累积完成后才返回，
        这样插件仍能沿用现有的结构化 JSON 校验和统一回复链路。
        """

        normalized_prompt = str(user_prompt or "").strip()
        if not normalized_prompt:
            raise PluginTextGenerationError("文本生成提示词不能为空")
        protocol = _normalize_protocol(api_protocol)
        normalized_reasoning = _normalize_reasoning_effort(reasoning_effort)
        messages: list[dict[str, Any]] = []
        normalized_system = str(system_prompt or "").strip()
        if normalized_system:
            messages.append({"role": "system", "content": normalized_system})
        content: str | list[dict[str, Any]] = normalized_prompt
        if images:
            if len(images) > 8:
                raise PluginTextGenerationError("一次最多分析 8 张图片或视频帧")
            content = [{"type": "text", "text": normalized_prompt}]
            for picture in images:
                if picture.mime_type not in {"image/jpeg", "image/png"} or not 0 < len(picture.data) <= 2 * 1024 * 1024:
                    raise PluginTextGenerationError("模型图片格式无效或超过 2 MiB")
                if picture.caption:
                    content.append({"type": "text", "text": picture.caption[:200]})
                encoded = base64.b64encode(picture.data).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": f"data:{picture.mime_type};base64,{encoded}"}})
        messages.append({"role": "user", "content": content})
        body = _build_request_body(
            protocol=protocol,
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            user_id=user_id,
            reasoning_effort=normalized_reasoning,
            stream=bool(stream),
        )
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Fulcrum/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.http_client.post_json(
                f"{self.api_base}/{protocol_endpoint(protocol)}",
                body,
                headers=headers,
                # HTTP 客户端把该值同时用作单次 socket 读超时：非流式的推理类模型要先思考再一次性
                # 返回，读超时若比总时限短（原先写死 10 秒）就会在模型还没答完时被误判为超时。
                connect_timeout_seconds=max(1, min(int(timeout_seconds), 300)),
                total_timeout_seconds=max(5, min(int(timeout_seconds), 300)),
                max_response_bytes=2 * 1024 * 1024,
                max_redirects=0 if images else 2,
            )
        except PluginHttpError as exc:
            raise PluginTextGenerationError("文本生成服务暂时不可用") from exc
        if stream:
            return _parse_stream_response(response, protocol=protocol)
        return _parse_generation_response(response, protocol=protocol)


def _parse_generation_response(
    response: PluginHttpResponse,
    *,
    protocol: str = "chat_completions",
) -> PluginTextGenerationResult:
    """严格解析 OpenAI-compatible 响应，不增加供应商私有字段回退。"""

    if response.status_code < 200 or response.status_code >= 300:
        raise PluginTextGenerationError(f"文本生成服务返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise PluginTextGenerationError("文本生成服务未返回合法 JSON") from exc
    if not isinstance(payload, Mapping):
        raise PluginTextGenerationError("文本生成服务响应不是对象")
    if protocol == "responses":
        text = _extract_responses_text(payload)
    else:
        text = _extract_chat_completion_text(payload)
    if not text:
        raise PluginTextGenerationError("文本生成服务未返回文本")
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    return PluginTextGenerationResult(
        text=text,
        token_usage=_bounded_nonnegative_int(usage.get("total_tokens"), maximum=10_000_000),
    )


def _build_request_body(
    *,
    protocol: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
    user_id: str,
    reasoning_effort: str | None,
    stream: bool,
) -> dict[str, Any]:
    """构造两个兼容协议的最小请求体，避免把供应商私有字段扩散到插件。"""

    normalized_max_tokens = max(1, min(int(max_tokens), 32_000))
    normalized_user = str(user_id or "plugin-user").strip() or "plugin-user"
    if protocol == "responses":
        input_items = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                content_items = [{"type": "input_text", "text": content}]
            else:
                content_items = []
                for item in content if isinstance(content, list) else []:
                    if item.get("type") == "text":
                        content_items.append({"type": "input_text", "text": str(item.get("text") or "")})
                    elif item.get("type") == "image_url":
                        image_url = item.get("image_url") if isinstance(item.get("image_url"), Mapping) else {}
                        content_items.append({"type": "input_image", "image_url": str(image_url.get("url") or "")})
            input_items.append({"role": message.get("role", "user"), "content": content_items})
        body: dict[str, Any] = {
            "model": model,
            "input": input_items,
            "max_output_tokens": normalized_max_tokens,
            "stream": bool(stream),
            "user": normalized_user,
        }
        # Responses 推理模型不接受 temperature；只有显式关闭推理时才省略字段。
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        return body
    body = {
        "model": model,
        "messages": messages,
        "temperature": max(0.0, min(float(temperature), 2.0)),
        "max_tokens": normalized_max_tokens,
        "stream": bool(stream),
        "user": normalized_user,
    }
    if stream:
        # OpenAI-compatible Chat Completions 只有显式开启后才会在末尾事件返回 usage。
        body["stream_options"] = {"include_usage": True}
    return body


def _parse_stream_response(response: PluginHttpResponse, *, protocol: str) -> PluginTextGenerationResult:
    """累积标准 SSE 文本增量；不把事件原文写入结果或日志。"""

    if response.status_code < 200 or response.status_code >= 300:
        raise PluginTextGenerationError(f"文本生成服务返回 HTTP {response.status_code}")
    text_parts: list[str] = []
    token_usage = 0
    saw_event = False
    for event in _iter_sse_payloads(response.text):
        saw_event = True
        event_type = str(event.get("type") or "")
        if (
            isinstance(event.get("error"), Mapping)
            or event_type.endswith(".error")
            or event_type in {"error", "response.failed", "response.incomplete"}
        ):
            raise PluginTextGenerationError("文本生成服务返回错误事件")
        if protocol == "responses":
            if event_type == "response.output_text.delta":
                text_parts.append(str(event.get("delta") or ""))
            completed = event.get("response") if event_type == "response.completed" else None
            if isinstance(completed, Mapping):
                usage = completed.get("usage") if isinstance(completed.get("usage"), Mapping) else {}
                token_usage = _bounded_nonnegative_int(usage.get("total_tokens"), maximum=10_000_000)
        else:
            choices = event.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
            delta = choice.get("delta") if isinstance(choice, Mapping) else {}
            if isinstance(delta, Mapping):
                value = delta.get("content")
                if isinstance(value, str):
                    text_parts.append(value)
            usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
            if usage:
                token_usage = _bounded_nonnegative_int(usage.get("total_tokens"), maximum=10_000_000)
    text = "".join(text_parts).strip()
    if not saw_event or not text:
        raise PluginTextGenerationError("文本生成服务未返回文本")
    return PluginTextGenerationResult(text=text, token_usage=token_usage)


def _iter_sse_payloads(raw_text: str):
    """逐事件读取 SSE data 行，忽略注释和供应商扩展字段。"""

    for line in str(raw_text or "").splitlines():
        normalized = line.strip()
        if not normalized or normalized.startswith(":") or not normalized.startswith("data:"):
            continue
        data = normalized[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except (TypeError, ValueError) as exc:
            raise PluginTextGenerationError("文本生成服务返回非法流式事件") from exc
        if not isinstance(payload, Mapping):
            raise PluginTextGenerationError("文本生成服务流式事件格式无效")
        yield payload


def _extract_chat_completion_text(payload: Mapping[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise PluginTextGenerationError("文本生成服务响应缺少 choices")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise PluginTextGenerationError("文本生成服务响应缺少 message")
    return str(message.get("content") or "").strip()


def _extract_responses_text(payload: Mapping[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = payload.get("output")
    parts: list[str] = []
    for item in output if isinstance(output, list) else []:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        for part in content if isinstance(content, list) else []:
            if isinstance(part, Mapping) and part.get("type") in {"output_text", "text"}:
                parts.append(str(part.get("text") or ""))
    return "".join(parts).strip()


def protocol_endpoint(protocol: str) -> str:
    """返回受控的 OpenAI-compatible 端点，禁止配置拼接任意路径。"""

    return "responses" if _normalize_protocol(protocol) == "responses" else "chat/completions"


def _normalize_protocol(value: Any) -> str:
    normalized = str(value or "chat_completions").strip().lower()
    if normalized not in {"responses", "chat_completions"}:
        return "chat_completions"
    return normalized


def _normalize_reasoning_effort(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {"low", "medium", "high"} else None


def _bounded_nonnegative_int(value: Any, *, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(normalized, maximum))
