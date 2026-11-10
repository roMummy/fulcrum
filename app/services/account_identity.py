"""接入账号(渠道)的统一身份:头像 + 昵称,wxid 作为弱化的第二行。

昵称来自渠道名(微桥会在手机回报 account_status 后自动把 wxid 换成昵称),
wxid 与头像来自渠道运行信息。页面一律用 _identity.html 的 identity 宏渲染,
不再各处自行拼接 wxid。
"""

from __future__ import annotations

import json
from typing import Any

from app.models.entities import Channel
from app.services.identity_directory import make_identity


def account_identity(channel: Channel) -> dict[str, Any]:
    """返回账号身份结构(见 identity_directory.make_identity):name 是昵称,external_id 是 wxid。"""

    try:
        runtime = json.loads(channel.runtime_info_json or "{}")
    except (TypeError, ValueError):
        runtime = {}
    runtime = runtime if isinstance(runtime, dict) else {}
    connection = runtime.get("account_connection") if isinstance(runtime.get("account_connection"), dict) else {}
    bridge = runtime.get("webridge") if isinstance(runtime.get("webridge"), dict) else {}
    profile = runtime.get("account_profile") if isinstance(runtime.get("account_profile"), dict) else {}
    account_id = str(connection.get("account_id") or bridge.get("account_id") or profile.get("account_id") or "").strip()
    name = str(connection.get("display_name") or channel.name or "").strip()
    return make_identity(account_id, name or channel.name, profile.get("avatar_url") or "")


def bot_identity(bot_name: str, channels: list[Channel]) -> dict[str, Any]:
    """机器人的身份:名称取机器人自己的名字,wxid 和头像取它的第一个接入账号(没有账号则只有名字)。"""

    if not channels:
        return make_identity("", bot_name, "")
    account = account_identity(channels[0])
    return make_identity(account["external_id"], bot_name, account["avatar_url"])
