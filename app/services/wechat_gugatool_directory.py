"""咕嘎助手群名册缓存与账号资料回填。

Agent 模式下平台不能主动访问手机,群资料只能通过 get_chatroom_info 命令在
事件入库后异步补齐。这里维护进程内缓存,避免同一条群消息反复触发命令。
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from app.core.time import now_shanghai
from app.models.entities import Channel

GROUP_TTL_SECONDS = 600
NEGATIVE_TTL_SECONDS = 120
REFRESH_TTL_SECONDS = 120
MAX_CACHE_ENTRIES = 4000

_cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
_cache_lock = threading.Lock()
_refresh_inflight: dict[tuple[int, str], float] = {}


def clear_cache() -> None:
    """测试和排障用：清空全部缓存。"""

    with _cache_lock:
        _cache.clear()
        _refresh_inflight.clear()


def _cache_get(key: tuple[Any, ...]) -> Any | None:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: tuple[Any, ...], value: Any, ttl: float) -> None:
    with _cache_lock:
        if len(_cache) >= MAX_CACHE_ENTRIES:
            overflow = max(1, MAX_CACHE_ENTRIES // 10)
            for stale_key in sorted(_cache, key=lambda item: _cache[item][0])[:overflow]:
                _cache.pop(stale_key, None)
        _cache[key] = (time.monotonic() + ttl, value)


def cached_group(channel_id: int, room_id: str) -> dict[str, Any] | None:
    """只读缓存里的群名册；消息入库阶段用它落群实体，不额外发请求。"""

    room = str(room_id or "").strip()
    if not room:
        return None
    return _cache_get(("group", int(channel_id or 0), room))


def cache_group(channel_id: int, room_id: str, *, title: str, members: dict[str, dict[str, Any]]) -> None:
    room = str(room_id or "").strip()
    if not room:
        return
    _cache_put(
        ("group", int(channel_id or 0), room),
        {"title": str(title or "").strip(), "members": members or {}},
        GROUP_TTL_SECONDS,
    )


def request_group_refresh(channel_id: int, room_id: str) -> bool:
    """登记一次名册刷新;同一群在 TTL 内只允许下发一条命令。"""

    room = str(room_id or "").strip()
    if not room:
        return False
    key = (int(channel_id or 0), room)
    now = time.monotonic()
    with _cache_lock:
        expires_at = _refresh_inflight.get(key, 0.0)
        if expires_at > now:
            return False
        _refresh_inflight[key] = now + REFRESH_TTL_SECONDS
        return True


def finish_group_refresh(channel_id: int, room_id: str) -> None:
    with _cache_lock:
        _refresh_inflight.pop((int(channel_id or 0), str(room_id or "").strip()), None)


def save_account_profile(channel: Channel, *, account_id: str, name: str, avatar_url: str) -> None:
    """把群名册里属于当前登录账号的昵称/头像写回运行信息。"""

    try:
        runtime = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        runtime = {}
    runtime = runtime if isinstance(runtime, dict) else {}
    normalized_name = str(name or "").strip()
    runtime["account_profile"] = {
        "account_id": str(account_id or "").strip(),
        "name": normalized_name,
        "avatar_url": str(avatar_url or "").strip(),
        "synced_at": now_shanghai().isoformat(),
    }
    connection = runtime.get("account_connection")
    if isinstance(connection, dict) and str(connection.get("account_id") or "").strip() == str(account_id or "").strip():
        if normalized_name:
            connection["display_name"] = normalized_name
        runtime["account_connection"] = connection
    channel.runtime_info_json = json.dumps(runtime, ensure_ascii=False, sort_keys=True)


def public_avatar_url(item: dict[str, Any]) -> str:
    """从手机回传的好友/群/成员资料里取可直接展示的头像地址。

    - 优先级:大头像 > 普通头像 > 小头像 > 手机本地下载地址;
    - 兼容旧版本把地址放在 avatar_url/head_img_url;
    - 手机本地回环/内网地址平台和浏览器都访问不到,直接丢弃;
    - 微信 CDN 统一升级到 https,避免后台是 https 时出现混合内容。
    """

    if not isinstance(item, dict):
        return ""
    for key in (
        "BigHeadImgUrl",
        "HeadImgUrl",
        "SmallHeadImgUrl",
        "big_head_img_url",
        "head_img_url",
        "small_head_img_url",
        "AvatarDownloadURL",
        "avatar_url",
        "avatar",
    ):
        value = str(item.get(key) or "").strip()
        if not value.startswith(("http://", "https://")):
            continue
        parsed = urlsplit(value)
        host = (parsed.hostname or "").strip().lower()
        if not host or host in {"localhost", "127.0.0.1", "::1"}:
            continue
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
            continue
        if parsed.scheme == "http" and host.endswith("qlogo.cn"):
            return "https://" + value[len("http://"):]
        return value
    return ""
