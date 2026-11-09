from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError, PluginHttpResponse


class PluginTextGenerationError(RuntimeError):
    """通用文本生成服务无法完成请求或返回了无效结果。"""


@dataclass(frozen=True)
class PluginTextGenerationResult:
    """插件可以持久化的最小生成结果，不包含供应商原始响应。"""

    text: str
    token_usage: int


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
    ) -> PluginTextGenerationResult:
        """调用标准 chat completion，并只解析首条文本与总 token 数。"""

        normalized_prompt = str(user_prompt or "").strip()
        if not normalized_prompt:
            raise PluginTextGenerationError("文本生成提示词不能为空")
        messages: list[dict[str, str]] = []
        normalized_system = str(system_prompt or "").strip()
        if normalized_system:
            messages.append({"role": "system", "content": normalized_system})
        messages.append({"role": "user", "content": normalized_prompt})
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "max_tokens": max(1, min(int(max_tokens), 32_000)),
            "stream": False,
            "user": str(user_id or "plugin-user").strip() or "plugin-user",
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = self.http_client.post_json(
                f"{self.api_base}/chat/completions",
                body,
                headers=headers,
                # HTTP 客户端把该值同时用作单次 socket 读超时：非流式的推理类模型要先思考再一次性
                # 返回，读超时若比总时限短（原先写死 10 秒）就会在模型还没答完时被误判为超时。
                connect_timeout_seconds=max(1, min(int(timeout_seconds), 300)),
                total_timeout_seconds=max(5, min(int(timeout_seconds), 300)),
                max_response_bytes=2 * 1024 * 1024,
                max_redirects=2,
            )
        except PluginHttpError as exc:
            raise PluginTextGenerationError("文本生成服务暂时不可用") from exc
        return _parse_generation_response(response)


def _parse_generation_response(response: PluginHttpResponse) -> PluginTextGenerationResult:
    """严格解析 OpenAI-compatible 响应，不增加供应商私有字段回退。"""

    if response.status_code < 200 or response.status_code >= 300:
        raise PluginTextGenerationError(f"文本生成服务返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise PluginTextGenerationError("文本生成服务未返回合法 JSON") from exc
    if not isinstance(payload, Mapping):
        raise PluginTextGenerationError("文本生成服务响应不是对象")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise PluginTextGenerationError("文本生成服务响应缺少 choices")
    message = choices[0].get("message")
    if not isinstance(message, Mapping):
        raise PluginTextGenerationError("文本生成服务响应缺少 message")
    text = str(message.get("content") or "").strip()
    if not text:
        raise PluginTextGenerationError("文本生成服务未返回文本")
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    return PluginTextGenerationResult(
        text=text,
        token_usage=_bounded_nonnegative_int(usage.get("total_tokens"), maximum=10_000_000),
    )


def _bounded_nonnegative_int(value: Any, *, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(normalized, maximum))
