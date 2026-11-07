from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from email.message import Message
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from app.core.logging import get_logger


logger = get_logger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_REDIRECTS = 5
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
}
SENSITIVE_QUERY_KEYWORDS = ("token", "key", "secret", "password", "signature", "authorization")
REDACTED_VALUE = "[REDACTED]"


class PluginHttpError(RuntimeError):
    """插件外部 HTTP 请求无法完成。"""


class PluginHttpTimeoutError(PluginHttpError):
    """插件外部 HTTP 请求超过连接或总时限。"""


class PluginHttpResponseTooLarge(PluginHttpError):
    """插件外部 HTTP 响应体超过允许大小。"""


class PluginHttpRedirectError(PluginHttpError):
    """插件外部 HTTP 重定向次数超过上限。"""


@dataclass(frozen=True)
class PluginHttpResponse:
    """统一保存插件可读取的 HTTP 响应，不暴露底层连接对象。"""

    status_code: int
    headers: Mapping[str, str]
    body: bytes
    url: str

    @property
    def text(self) -> str:
        """按照响应 charset 解码文本，缺少声明时使用 UTF-8 替换非法字节。"""

        content_type = str(self.headers.get("content-type") or "")
        charset = "utf-8"
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "charset" and value.strip():
                charset = value.strip().strip('"')
                break
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """解析 JSON；格式错误直接交给调用插件处理，不做空对象降级。"""

        return json.loads(self.text)


class PluginHttpClient:
    """为租户插件提供有限重定向、有限响应体和幂等重试的 HTTP 客户端。"""

    def __init__(
        self,
        *,
        opener_factory: Callable[[int], OpenerDirector] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._opener_factory = opener_factory or _build_limited_opener
        self._clock = clock

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | str | None = None,
        json_body: Any = None,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_redirects: int = 3,
        retry_count: int = 0,
    ) -> PluginHttpResponse:
        """执行一次受控 HTTP 请求，只有 GET 和 HEAD 可以自动重试。"""

        normalized_method = str(method or "GET").strip().upper()
        if normalized_method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise PluginHttpError("插件 HTTP 方法不受支持")
        normalized_url = validate_http_url(url)
        normalized_headers = {str(key): str(value) for key, value in dict(headers or {}).items()}
        encoded_body = _encode_request_body(body=body, json_body=json_body, headers=normalized_headers)
        connect_timeout = _bounded_float(connect_timeout_seconds, minimum=0.1, maximum=60.0)
        total_timeout = _bounded_float(total_timeout_seconds, minimum=0.1, maximum=300.0)
        response_limit = max(1, min(int(max_response_bytes), MAX_RESPONSE_BYTES))
        redirect_limit = max(0, min(int(max_redirects), MAX_REDIRECTS))
        retries = max(0, min(int(retry_count), 2)) if normalized_method in {"GET", "HEAD"} else 0

        request = Request(
            normalized_url,
            data=encoded_body,
            headers=normalized_headers,
            method=normalized_method,
        )
        deadline = self._clock() + total_timeout
        attempts = retries + 1
        last_error: BaseException | None = None

        logger.debug(
            "plugin http request started",
            extra={
                "method": normalized_method,
                "url": redact_url(normalized_url),
                "headers": sanitize_http_headers(normalized_headers),
                "max_response_bytes": response_limit,
            },
        )
        for attempt in range(attempts):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise PluginHttpTimeoutError("插件 HTTP 请求超过总时限") from last_error
            opener = self._opener_factory(redirect_limit)
            try:
                response = opener.open(request, timeout=min(connect_timeout, remaining))
                with response:
                    return _read_response(
                        response,
                        max_response_bytes=response_limit,
                        deadline=deadline,
                        clock=self._clock,
                    )
            except HTTPError as exc:
                # HTTPError 同时也是可读取的响应对象。4xx/5xx 由插件按业务语义判断，
                # 平台仍然执行相同的响应体上限，避免错误页绕过内存保护。
                with exc:
                    return _read_response(
                        exc,
                        max_response_bytes=response_limit,
                        deadline=deadline,
                        clock=self._clock,
                    )
            except PluginHttpError:
                raise
            except (socket.timeout, TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise PluginHttpTimeoutError("插件 HTTP 请求超时") from exc
            except URLError as exc:
                last_error = exc
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    if attempt + 1 >= attempts:
                        raise PluginHttpTimeoutError("插件 HTTP 请求超时") from exc
                elif attempt + 1 >= attempts:
                    raise PluginHttpError("插件 HTTP 请求连接失败") from exc
            except OSError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise PluginHttpError("插件 HTTP 请求失败") from exc

        raise PluginHttpError("插件 HTTP 请求失败") from last_error

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """执行 GET 并直接解析 JSON，保留 request 的全部安全限制参数。"""

        return self.request("GET", url, **kwargs).json()

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> PluginHttpResponse:
        """执行不自动重试的 JSON POST，避免重复触发外部副作用。"""

        return self.request("POST", url, json_body=payload, **kwargs)


class _LimitedRedirectHandler(HTTPRedirectHandler):
    """按单次 opener 记录重定向次数，超过上限立即中止。"""

    def __init__(self, max_redirects: int):
        super().__init__()
        self.max_redirects = max_redirects
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        self.redirect_count += 1
        if self.redirect_count > self.max_redirects:
            raise PluginHttpRedirectError("插件 HTTP 重定向次数超过上限")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_http_url(url: str) -> str:
    """只接受无内嵌凭证的 HTTP/HTTPS 地址。"""

    normalized = str(url or "").strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise PluginHttpError("插件 HTTP 地址必须使用 http 或 https")
    if parsed.username is not None or parsed.password is not None:
        raise PluginHttpError("插件 HTTP 地址不能内嵌账号密码")
    return normalized


def sanitize_http_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """生成可记录的请求头副本，敏感值始终替换为固定占位符。"""

    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        normalized_key = str(key)
        lowered = normalized_key.lower()
        if lowered in SENSITIVE_HEADER_NAMES or any(word in lowered for word in ("token", "secret", "password")):
            sanitized[normalized_key] = REDACTED_VALUE
        else:
            sanitized[normalized_key] = str(value)
    return sanitized


def redact_url(url: str) -> str:
    """脱敏 URL 查询参数，日志中不保留 token、key 或签名原值。"""

    parsed = urlsplit(url)
    redacted_query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        redacted_query.append(
            (key, REDACTED_VALUE if any(word in lowered for word in SENSITIVE_QUERY_KEYWORDS) else value)
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(redacted_query), parsed.fragment))


def _build_limited_opener(max_redirects: int) -> OpenerDirector:
    return build_opener(_LimitedRedirectHandler(max_redirects))


def _encode_request_body(
    *,
    body: bytes | str | None,
    json_body: Any,
    headers: dict[str, str],
) -> bytes | None:
    if body is not None and json_body is not None:
        raise PluginHttpError("body 与 json_body 不能同时设置")
    if json_body is not None:
        headers.setdefault("Content-Type", "application/json; charset=utf-8")
        return json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if body is None:
        return None
    return body if isinstance(body, bytes) else str(body).encode("utf-8")


def _read_response(
    response: Any,
    *,
    max_response_bytes: int,
    deadline: float,
    clock: Callable[[], float],
) -> PluginHttpResponse:
    headers = _normalize_response_headers(getattr(response, "headers", {}))
    content_length = _parse_content_length(headers.get("content-length"))
    if content_length is not None and content_length > max_response_bytes:
        raise PluginHttpResponseTooLarge("插件 HTTP 响应体超过允许大小")

    chunks: list[bytes] = []
    total_size = 0
    while True:
        if clock() >= deadline:
            raise PluginHttpTimeoutError("插件 HTTP 请求超过总时限")
        chunk = response.read(min(64 * 1024, max_response_bytes - total_size + 1))
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_response_bytes:
            raise PluginHttpResponseTooLarge("插件 HTTP 响应体超过允许大小")
        chunks.append(chunk)

    status_code = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
    final_url = str(response.geturl() if hasattr(response, "geturl") else "")
    return PluginHttpResponse(
        status_code=status_code,
        headers=headers,
        body=b"".join(chunks),
        url=final_url,
    )


def _normalize_response_headers(headers: Mapping[str, Any] | Message) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def _parse_content_length(value: str | None) -> int | None:
    try:
        parsed = int(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _bounded_float(value: float, *, minimum: float, maximum: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise PluginHttpError("插件 HTTP 超时参数不合法") from exc
    return max(minimum, min(normalized, maximum))
