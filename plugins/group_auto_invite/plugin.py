from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.entities import Channel
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.wechat_ipad.client import WechatIpadApiClient


CONFIG_PREFIX = "#加群配置"
JOIN_PREFIX = "#加群"


@dataclass(frozen=True)
class InviteConfig:
    enabled: bool
    admin_user_ids: tuple[str, ...]
    mappings: dict[str, list[str]]
    invite_directly: bool
    require_direct_chat: bool
    response_prefix: str


def matches_event(context) -> bool | dict[str, object]:
    content = str(getattr(context, "content", "") or "").strip()
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    if content.startswith(CONFIG_PREFIX):
        return {"matched": True, "reason": "invite_config_command", "debug": {"chat_type": chat_type}}
    if content.startswith(JOIN_PREFIX):
        return {"matched": True, "reason": "invite_join_command", "debug": {"chat_type": chat_type}}
    return {"matched": False, "reason": "not_invite_command", "debug": {"chat_type": chat_type}}


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)
    channel_type = str(request.payload.get("channel_type") or "").strip().lower()

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="group_auto_invite runtime is ready",
            output={
                "enabled": config.enabled,
                "mapping_count": len(config.mappings),
                "admin_user_ids": list(config.admin_user_ids),
                "invite_directly": config.invite_directly,
                "require_direct_chat": config.require_direct_chat,
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")
    if not config.enabled:
        return PluginExecutionResult(ok=False, status="disabled", message="group_auto_invite is disabled")
    if channel_type and channel_type != "wechat_ipad":
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="group_auto_invite only supports wechat_ipad",
            output={"provider_status": "ignored", "channel_type": channel_type},
        )

    content = str(request.payload.get("content") or "").strip()
    if content.startswith(CONFIG_PREFIX):
        return _handle_config_command(request, config)
    if content.startswith(JOIN_PREFIX):
        return _handle_join_command(request, config)
    return PluginExecutionResult(ok=True, status="completed", message="not a group invite command", output={"provider_status": "ignored"})


def _handle_config_command(request: PluginExecutionRequest, config: InviteConfig) -> PluginExecutionResult:
    sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
    if config.admin_user_ids and sender_id not in config.admin_user_ids:
        return PluginExecutionResult(ok=False, status="forbidden", message="only configured admins can manage invite mappings")

    command_text = str(request.payload.get("content") or "").strip()
    body = command_text[len(CONFIG_PREFIX) :].strip()
    if body.startswith("|"):
        body = body[1:].strip()
    parts = body.split()
    if not parts:
        return _help_result(config)

    action = parts[0].lower()
    mappings = _clone_mappings(config.mappings)

    if action == "help":
        return _help_result(config)
    if action == "list":
        return _list_result(mappings)
    if action == "add" and len(parts) >= 3:
        key = parts[1].strip().lower()
        chatroom_id = parts[2].strip()
        if not key or not chatroom_id:
            return PluginExecutionResult(ok=False, status="error", message="usage: #加群配置 add key chatroom_id")
        items = mappings.setdefault(key, [])
        if chatroom_id not in items:
            items.append(chatroom_id)
        return _config_update_result(mappings, f"已添加映射: {key} -> {chatroom_id}")
    if action == "del" and len(parts) >= 3:
        key = parts[1].strip().lower()
        chatroom_id = parts[2].strip()
        items = mappings.get(key, [])
        if chatroom_id in items:
            items.remove(chatroom_id)
        if not items and key in mappings:
            mappings.pop(key, None)
        return _config_update_result(mappings, f"已删除映射: {key} -> {chatroom_id}")
    if action == "clear" and len(parts) >= 2:
        key = parts[1].strip().lower()
        mappings.pop(key, None)
        return _config_update_result(mappings, f"已清空关键字: {key}")
    if action == "get" and len(parts) >= 2:
        key = parts[1].strip().lower()
        items = mappings.get(key, [])
        if not items:
            return PluginExecutionResult(ok=True, status="completed", message=f"关键字 {key} 没有关联群")
        text = f"{key} -> {', '.join(items)}"
        return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "mapping_found", "reply_text": text, "replies": [{"type": "text", "content": text}]})

    return PluginExecutionResult(ok=False, status="error", message="unknown config command, use #加群配置 help")


def _handle_join_command(request: PluginExecutionRequest, config: InviteConfig) -> PluginExecutionResult:
    content = str(request.payload.get("content") or "").strip()
    parts = content.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return PluginExecutionResult(ok=False, status="error", message="usage: #加群 key")

    normalized_event = request.payload.get("normalized_event")
    conversation = normalized_event.get("conversation") if isinstance(normalized_event, dict) else {}
    chat_type = str((conversation or {}).get("chat_type") or request.payload.get("chat_type") or "").strip().lower()
    if config.require_direct_chat and chat_type == "group":
        text = "请私聊机器人发送 `#加群 key`。"
        return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "direct_chat_required", "reply_text": text, "replies": [{"type": "text", "content": text}]})

    key = parts[1].strip().lower()
    chatroom_ids = config.mappings.get(key, [])
    if not chatroom_ids:
        text = f"未找到关键字 `{key}` 对应的群聊。"
        return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "mapping_missing", "reply_text": text, "replies": [{"type": "text", "content": text}]})

    channel_id = _coerce_int(request.payload.get("channel_id"))
    sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
    first_chatroom_id = chatroom_ids[0]
    invite_result = None
    if config.invite_directly and channel_id and sender_id:
        invite_result = _invite_user(channel_id, sender_id=sender_id, chatroom_id=first_chatroom_id)

    if invite_result and invite_result.get("ok"):
        text = f"{config.response_prefix}已向你发送群邀请，请留意微信通知。"
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=text,
            output={
                "provider_status": "invite_sent",
                "chatroom_id": first_chatroom_id,
                "chatroom_ids": chatroom_ids,
                "invite_result": invite_result,
                "reply_text": text,
                "replies": [{"type": "text", "content": text}],
            },
        )

    text = f"{config.response_prefix}关键字 `{key}` 对应群: {', '.join(chatroom_ids)}。"
    if invite_result and not invite_result.get("ok"):
        text = f"{text}\n自动邀请失败: {invite_result.get('message') or 'unknown error'}"
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text,
        output={
            "provider_status": "invite_guided" if not invite_result else "invite_failed_guided",
            "chatroom_id": first_chatroom_id,
            "chatroom_ids": chatroom_ids,
            "invite_result": invite_result or {},
            "reply_text": text,
            "replies": [{"type": "text", "content": text}],
        },
    )


def _invite_user(channel_id: int, *, sender_id: str, chatroom_id: str) -> dict[str, object]:
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        if channel is None:
            return {"ok": False, "message": "channel not found"}
        if str(channel.channel_type or "").strip() != "wechat_ipad":
            return {"ok": False, "message": "channel type does not support direct invite"}
        adapter_config = _parse_json(getattr(channel, "adapter_config_json", None))
        wxid = str(adapter_config.get("wxid") or "").strip()
        if not wxid:
            return {"ok": False, "message": "channel wxid is missing"}
        client = WechatIpadApiClient()
        response = client.invite_chatroom_member(
            channel,
            wxid=wxid,
            chatroom_id=chatroom_id,
            to_wxids=[sender_id],
        )
    ok = _response_ok(response)
    return {
        "ok": ok,
        "message": str(response.get("Msg") or response.get("message") or ("ok" if ok else "invite failed")),
        "provider_response": response,
    }


def _help_result(config: InviteConfig) -> PluginExecutionResult:
    text = (
        "加群配置命令:\n"
        "#加群配置 help\n"
        "#加群配置 list\n"
        "#加群配置 add key room@chatroom\n"
        "#加群配置 del key room@chatroom\n"
        "#加群配置 clear key\n"
        "#加群配置 get key\n\n"
        "用户命令:\n"
        "#加群 key"
    )
    return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "help", "mapping_count": len(config.mappings), "reply_text": text, "replies": [{"type": "text", "content": text}]})


def _list_result(mappings: dict[str, list[str]]) -> PluginExecutionResult:
    if not mappings:
        text = "当前没有配置任何加群映射。"
    else:
        text = "\n".join(f"{key} -> {', '.join(value)}" for key, value in sorted(mappings.items()))
    return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "list", "mappings": mappings, "reply_text": text, "replies": [{"type": "text", "content": text}]})


def _config_update_result(mappings: dict[str, list[str]], message: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": "config_updated",
            "config_patch": {"mappings": mappings},
            "mappings": mappings,
            "reply_text": message,
            "replies": [{"type": "text", "content": message}],
        },
    )


def _load_config(request: PluginExecutionRequest) -> InviteConfig:
    raw = dict(request.config)
    mappings_raw = raw.get("mappings")
    mappings: dict[str, list[str]] = {}
    if isinstance(mappings_raw, dict):
        for key, value in mappings_raw.items():
            normalized_key = str(key or "").strip().lower()
            if not normalized_key:
                continue
            if isinstance(value, list):
                items = [str(item or "").strip() for item in value if str(item or "").strip()]
            else:
                one = str(value or "").strip()
                items = [one] if one else []
            if items:
                mappings[normalized_key] = items
    admins_raw = raw.get("admin_user_ids")
    admin_user_ids = tuple(str(item or "").strip() for item in admins_raw) if isinstance(admins_raw, list) else ()
    return InviteConfig(
        enabled=_as_bool(raw.get("enabled"), default=True),
        admin_user_ids=tuple(item for item in admin_user_ids if item),
        mappings=mappings,
        invite_directly=_as_bool(raw.get("invite_directly"), default=True),
        require_direct_chat=_as_bool(raw.get("require_direct_chat"), default=True),
        response_prefix=str(raw.get("response_prefix") or "").strip(),
    )


def _response_ok(response: dict[str, Any]) -> bool:
    if not isinstance(response, dict):
        return False
    ret = response.get("Ret")
    if ret is not None:
        try:
            return int(ret) == 0
        except (TypeError, ValueError):
            return False
    if response.get("ok") is not None:
        return bool(response.get("ok"))
    status = int(response.get("http_status") or 200)
    return status < 400


def _clone_mappings(mappings: dict[str, list[str]]) -> dict[str, list[str]]:
    return {key: list(value) for key, value in mappings.items()}


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _as_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_int(value: object) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
