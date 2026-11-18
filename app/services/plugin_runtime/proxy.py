"""插件绑定级出网代理的解析、绕过规则和单次执行上下文。"""

from __future__ import annotations

import ipaddress
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Mapping
from urllib.parse import quote, unquote, urlsplit


MAX_BYPASS_ENTRIES = 100
DEFAULT_BYPASS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
PROXY_SCHEMES = frozenset({"http", "socks5", "socks5h"})
BINDING_PROXY_KEYS = frozenset({"proxy_url", "proxy_username", "proxy_password", "proxy_bypass"})
GROUP_PROXY_KEYS = frozenset({
    "group_proxy_mode",
    "group_proxy_url",
    "group_proxy_username",
    "group_proxy_password",
    "group_proxy_bypass",
})
RESERVED_PROXY_KEYS = BINDING_PROXY_KEYS | GROUP_PROXY_KEYS
_BYPASS_SPLIT = re.compile(r"[,\s]+")


class PluginProxyConfigError(ValueError):
    """插件代理配置不完整或不合法。"""


@dataclass(frozen=True)
class PluginProxyEndpoint:
    """一次插件执行使用的标准化代理端点，密码只保留在内存中。"""

    proxy_type: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    bypass: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        """HTTP 代理地址；用户名和密码按 URL 规则编码。"""

        credentials = ""
        if self.username:
            credentials = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return f"http://{credentials}{_format_host(self.host)}:{self.port}"

    @property
    def safe_label(self) -> str:
        """日志可用的代理标识，不包含用户名和密码。"""

        return f"{self.proxy_type}://{_format_host(self.host)}:{self.port}"

    def bypasses(self, host: str | None) -> bool:
        """判断目标主机是否应绕过代理；重定向后的新主机同样适用。"""

        normalized = _normalize_host(host)
        if not normalized:
            return False
        if normalized in DEFAULT_BYPASS_HOSTS:
            return True
        try:
            if ipaddress.ip_address(normalized).is_loopback:
                return True
        except ValueError:
            pass
        for pattern in self.bypass:
            if pattern == "*":
                return True
            candidate = pattern.lstrip(".").lower()
            if normalized == candidate or normalized.endswith("." + candidate):
                return True
        return False


_CURRENT_PROXY: ContextVar[PluginProxyEndpoint | None] = ContextVar("plugin_http_proxy", default=None)


@contextmanager
def plugin_proxy_scope(endpoint: PluginProxyEndpoint | None) -> Iterator[None]:
    """在插件执行期间提供代理端点，退出时恢复上一层的值。"""

    token = _CURRENT_PROXY.set(endpoint)
    try:
        yield
    finally:
        _CURRENT_PROXY.reset(token)


def current_plugin_proxy() -> PluginProxyEndpoint | None:
    """返回当前插件执行上下文中的代理端点。"""

    return _CURRENT_PROXY.get()


def proxy_request_host(url: str) -> str | None:
    """从请求 URL 提取 hostname，避免把端口混进绕过规则。"""

    return urlsplit(str(url or "")).hostname


def normalize_bypass(value: Any) -> tuple[str, ...]:
    """把逗号、空格或换行分隔的绕过列表收敛为去重后的主机规则。"""

    if isinstance(value, (list, tuple)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = _BYPASS_SPLIT.split(str(value or ""))
    normalized: list[str] = []
    for raw in raw_items:
        item = str(raw or "").strip().lower()
        if item.startswith("*."):
            item = item[2:]
        item = item.strip(".")
        if not item:
            continue
        if item != "*" and ("://" in item or "@" in item or "/" in item):
            raise PluginProxyConfigError("proxy_bypass_invalid")
        if item not in normalized:
            normalized.append(item)
    if len(normalized) > MAX_BYPASS_ENTRIES:
        raise PluginProxyConfigError("proxy_bypass_invalid")
    return tuple(normalized)


def parse_proxy_url(value: Any, *, bypass: Any = "") -> PluginProxyEndpoint | None:
    """解析旧的单 URL 代理格式（允许 URL 内嵌账号密码）。"""

    normalized = str(value or "").strip()
    if not normalized:
        return None
    parsed = _parse_proxy_parts(normalized)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if bool(username) != bool(password):
        raise PluginProxyConfigError("proxy_url_credentials_incomplete")
    return _build_endpoint(parsed, username=username, password=password, bypass=bypass)


def parse_proxy_config(
    proxy_url: Any,
    *,
    username: Any = "",
    password: Any = "",
    bypass: Any = "",
) -> PluginProxyEndpoint | None:
    """解析新的拆分代理配置：URL 只含类型/主机/端口，认证单独提供。"""

    normalized = str(proxy_url or "").strip()
    if not normalized:
        return None
    parsed = _parse_proxy_parts(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise PluginProxyConfigError("proxy_url_credentials_inline")
    normalized_username = str(username or "").strip()
    normalized_password = str(password or "")
    if bool(normalized_username) != bool(normalized_password):
        raise PluginProxyConfigError("proxy_url_credentials_incomplete")
    return _build_endpoint(
        parsed,
        username=normalized_username,
        password=normalized_password,
        bypass=bypass,
    )


def _parse_proxy_parts(value: str):
    parsed = urlsplit(value)
    if "://" not in value:
        raise PluginProxyConfigError("proxy_url_scheme_required")
    if parsed.scheme.lower() not in PROXY_SCHEMES:
        raise PluginProxyConfigError("proxy_url_scheme_invalid")
    if not parsed.hostname:
        raise PluginProxyConfigError("proxy_url_host_required")
    try:
        port = parsed.port
    except ValueError as exc:
        raise PluginProxyConfigError("proxy_url_port_invalid") from exc
    if port is None or port < 1 or port > 65535:
        raise PluginProxyConfigError("proxy_url_port_invalid")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise PluginProxyConfigError("proxy_url_invalid")
    return parsed


def _build_endpoint(
    parsed,
    *,
    username: str,
    password: str,
    bypass: Any,
) -> PluginProxyEndpoint:
    return PluginProxyEndpoint(
        proxy_type=parsed.scheme.lower(),
        host=parsed.hostname or "",
        port=int(parsed.port or 0),
        username=username,
        password=password,
        bypass=normalize_bypass(bypass),
    )


def strip_proxy_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """移除绑定级代理配置键，避免进入插件或群级可见配置。"""

    return {key: value for key, value in config.items() if key not in RESERVED_PROXY_KEYS}


def _normalize_host(host: str | None) -> str:
    normalized = str(host or "").strip().lower().rstrip(".")
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized


def _format_host(host: str) -> str:
    normalized = str(host or "").strip()
    if ":" in normalized and not normalized.startswith("["):
        return f"[{normalized}]"
    return normalized
