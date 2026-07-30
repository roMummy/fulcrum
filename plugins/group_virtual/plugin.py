from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.entities import Channel, Chatroom
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.wechat_ipad.client import WechatIpadApiClient


CONFIG_PREFIX = "#群虚拟"
FORWARD_PREFIX = "[群桥]"


@dataclass(frozen=True)
class GroupVirtualConfig:
    enabled: bool
    admin_user_ids: tuple[str, ...]
    bridges: dict[str, list[str]]
    prefix_template: str


def matches_event(context) -> bool | dict[str, object]:
    content = str(getattr(context, "content", "") or "").strip()
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    channel_type = str(getattr(context, "channel_type", "") or "").strip().lower()
    conversation_external_id = str(getattr(context, "conversation_external_id", "") or "").strip()

    if channel_type != "wechat_ipad":
        return {"matched": False, "reason": "channel_not_supported", "debug": {"channel_type": channel_type}}
    if content.startswith(CONFIG_PREFIX):
        return {"matched": True, "reason": "group_virtual_config", "debug": {"chat_type": chat_type}}
    if chat_type == "group" and conversation_external_id.endswith("@chatroom"):
        return {"matched": True, "reason": "group_message_candidate", "debug": {"chat_type": chat_type}}
    return {"matched": False, "reason": "not_virtual_group_event", "debug": {"chat_type": chat_type}}


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request)
    channel_type = str(request.payload.get("channel_type") or "").strip().lower()

    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="group_virtual runtime is ready",
            output={
                "enabled": config.enabled,
                "bridge_count": len(config.bridges),
                "admin_user_ids": list(config.admin_user_ids),
                "prefix_template": config.prefix_template,
            },
        )

    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")
    if not config.enabled:
        return PluginExecutionResult(ok=False, status="disabled", message="group_virtual is disabled")
    if channel_type and channel_type != "wechat_ipad":
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="group_virtual only supports wechat_ipad",
            output={"provider_status": "ignored", "channel_type": channel_type},
        )

    content = str(request.payload.get("content") or "").strip()
    if content.startswith(CONFIG_PREFIX):
        return _handle_config_command(request, config)
    return _handle_forward(request, config)


def _handle_config_command(request: PluginExecutionRequest, config: GroupVirtualConfig) -> PluginExecutionResult:
    sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
    if config.admin_user_ids and sender_id not in config.admin_user_ids:
        return PluginExecutionResult(ok=False, status="forbidden", message="only configured admins can manage virtual bridges")

    content = str(request.payload.get("content") or "").strip()
    body = content[len(CONFIG_PREFIX) :].strip()
    if body.startswith("|"):
        body = body[1:].strip()
    parts = body.split()
    if not parts:
        return _help_result(config)

    action = parts[0].lower()
    bridges = _clone_map(config.bridges)

    if action == "help":
        return _help_result(config)
    if action == "list":
        return _list_result(bridges)
    if action == "add" and len(parts) >= 4:
        bridge_name = parts[1].strip().lower()
        group_ids = _normalize_group_ids(parts[2:])
        if len(group_ids) < 2:
            return PluginExecutionResult(ok=False, status="error", message="a virtual bridge needs at least 2 groups")
        bridges[bridge_name] = group_ids
        return _config_update_result(bridges, f"已保存虚拟群桥 {bridge_name}: {', '.join(group_ids)}")
    if action == "del" and len(parts) >= 2:
        bridge_name = parts[1].strip().lower()
        bridges.pop(bridge_name, None)
        return _config_update_result(bridges, f"已删除虚拟群桥 {bridge_name}")
    if action == "get" and len(parts) >= 2:
        bridge_name = parts[1].strip().lower()
        group_ids = bridges.get(bridge_name, [])
        if not group_ids:
            return PluginExecutionResult(ok=True, status="completed", message=f"未找到虚拟群桥 {bridge_name}")
        text = f"{bridge_name} -> {', '.join(group_ids)}"
        return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "bridge_found", "bridge_name": bridge_name, "group_ids": group_ids, "reply_text": text, "replies": [{"type": "text", "content": text}]})

    return PluginExecutionResult(ok=False, status="error", message="unknown command, use #群虚拟 help")


def _handle_forward(request: PluginExecutionRequest, config: GroupVirtualConfig) -> PluginExecutionResult:
    payload = dict(request.payload)
    normalized_event = payload.get("normalized_event")
    conversation = normalized_event.get("conversation") if isinstance(normalized_event, dict) else {}
    details = normalized_event.get("details") if isinstance(normalized_event, dict) else {}

    chat_type = str((conversation or {}).get("chat_type") or payload.get("chat_type") or "").strip().lower()
    if chat_type != "group":
        return PluginExecutionResult(ok=True, status="completed", message="not a group chat message", output={"provider_status": "ignored"})
    if str(payload.get("message_type") or "").strip().lower() != "text":
        return PluginExecutionResult(ok=True, status="completed", message="non-text forwarding skipped", output={"provider_status": "ignored"})
    if str((details or {}).get("direction") or "in").strip().lower() != "in":
        return PluginExecutionResult(ok=True, status="completed", message="outbound event ignored", output={"provider_status": "ignored"})

    source_group_id = str(payload.get("conversation_external_id") or (conversation or {}).get("external_id") or "").strip()
    if not source_group_id.endswith("@chatroom"):
        return PluginExecutionResult(ok=True, status="completed", message="source is not a chatroom", output={"provider_status": "ignored"})

    sender_id = str(payload.get("sender_id") or "").strip()
    self_wxid = str((details or {}).get("self_wxid") or "").strip()
    if sender_id and self_wxid and sender_id == self_wxid:
        return PluginExecutionResult(ok=True, status="completed", message="self message ignored", output={"provider_status": "ignored"})

    content = str(payload.get("content") or "").strip()
    if not content or content.startswith(FORWARD_PREFIX):
        return PluginExecutionResult(ok=True, status="completed", message="empty or already forwarded message", output={"provider_status": "ignored"})

    bridge_name, target_group_ids = _resolve_targets(config.bridges, source_group_id)
    if not target_group_ids:
        return PluginExecutionResult(ok=True, status="completed", message="source group not configured in any bridge", output={"provider_status": "ignored"})

    channel_id = _coerce_int(payload.get("channel_id"))
    if not channel_id:
        return PluginExecutionResult(ok=False, status="error", message="channel_id missing")

    sender_name = str(payload.get("sender_name") or sender_id or "成员").strip() or "成员"
    source_group_name = _resolve_group_name(channel_id, source_group_id, fallback=str((conversation or {}).get("title") or "").strip())
    forward_text = config.prefix_template.format(sender=sender_name, group=source_group_name) + content
    results = _forward_text(channel_id, target_group_ids=target_group_ids, text=forward_text)
    success_count = sum(1 for item in results if item.get("ok"))

    return PluginExecutionResult(
        ok=success_count > 0,
        status="completed" if success_count > 0 else "error",
        message=f"group virtual forwarded to {success_count}/{len(target_group_ids)} groups",
        output={
            "provider_status": "forwarded" if success_count > 0 else "forward_failed",
            "bridge_name": bridge_name,
            "source_group_id": source_group_id,
            "target_group_ids": target_group_ids,
            "forward_text": forward_text,
            "forward_results": results,
        },
    )


def _forward_text(channel_id: int, *, target_group_ids: list[str], text: str) -> list[dict[str, object]]:
    with SessionLocal() as db:
        channel = db.get(Channel, channel_id)
        if channel is None:
            return [{"ok": False, "message": "channel not found"}]
        adapter_config = _parse_json(getattr(channel, "adapter_config_json", None))
        wxid = str(adapter_config.get("wxid") or "").strip()
        if not wxid:
            return [{"ok": False, "message": "channel wxid is missing"}]
        client = WechatIpadApiClient()
        results: list[dict[str, object]] = []
        for target_group_id in target_group_ids:
            response = client.send_text_message(
                channel,
                wxid=wxid,
                to_wxid=target_group_id,
                content=text,
            )
            results.append(
                {
                    "target_group_id": target_group_id,
                    "ok": _response_ok(response),
                    "provider_response": response,
                }
            )
        return results


def _resolve_targets(bridges: dict[str, list[str]], source_group_id: str) -> tuple[str, list[str]]:
    for bridge_name, group_ids in bridges.items():
        if source_group_id in group_ids:
            return bridge_name, [item for item in group_ids if item != source_group_id]
    return "", []


def _resolve_group_name(channel_id: int, group_id: str, *, fallback: str) -> str:
    with SessionLocal() as db:
        chatroom = db.scalar(
            select(Chatroom).where(
                Chatroom.channel_id == channel_id,
                Chatroom.external_id == group_id,
            )
        )
        if chatroom and str(chatroom.name or "").strip():
            return str(chatroom.name).strip()
    return fallback or group_id


def _help_result(config: GroupVirtualConfig) -> PluginExecutionResult:
    text = (
        "虚拟群桥命令:\n"
        "#群虚拟 help\n"
        "#群虚拟 list\n"
        "#群虚拟 add bridge room1@chatroom room2@chatroom [room3@chatroom]\n"
        "#群虚拟 get bridge\n"
        "#群虚拟 del bridge"
    )
    return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "help", "bridge_count": len(config.bridges), "reply_text": text, "replies": [{"type": "text", "content": text}]})


def _list_result(bridges: dict[str, list[str]]) -> PluginExecutionResult:
    if not bridges:
        text = "当前没有配置任何虚拟群桥。"
    else:
        text = "\n".join(f"{name} -> {', '.join(group_ids)}" for name, group_ids in sorted(bridges.items()))
    return PluginExecutionResult(ok=True, status="completed", message=text, output={"provider_status": "list", "bridges": bridges, "reply_text": text, "replies": [{"type": "text", "content": text}]})


def _config_update_result(bridges: dict[str, list[str]], message: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": "config_updated",
            "config_patch": {"bridges": bridges},
            "bridges": bridges,
            "reply_text": message,
            "replies": [{"type": "text", "content": message}],
        },
    )


def _load_config(request: PluginExecutionRequest) -> GroupVirtualConfig:
    raw = dict(request.config)
    bridges_raw = raw.get("bridges")
    bridges: dict[str, list[str]] = {}
    if isinstance(bridges_raw, dict):
        for key, value in bridges_raw.items():
            normalized_key = str(key or "").strip().lower()
            if not normalized_key:
                continue
            if isinstance(value, list):
                items = _normalize_group_ids(value)
            else:
                items = _normalize_group_ids([value])
            if len(items) >= 2:
                bridges[normalized_key] = items
    admins_raw = raw.get("admin_user_ids")
    admin_user_ids = tuple(str(item or "").strip() for item in admins_raw) if isinstance(admins_raw, list) else ()
    return GroupVirtualConfig(
        enabled=_as_bool(raw.get("enabled"), default=True),
        admin_user_ids=tuple(item for item in admin_user_ids if item),
        bridges=bridges,
        prefix_template=str(raw.get("prefix_template") or f"{FORWARD_PREFIX}{{sender}}@{{group}}: ").strip() or f"{FORWARD_PREFIX}{{sender}}@{{group}}: ",
    )


def _normalize_group_ids(values: list[object]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        group_id = str(item or "").strip()
        if not group_id or not group_id.endswith("@chatroom") or group_id in seen:
            continue
        seen.add(group_id)
        result.append(group_id)
    return result


def _clone_map(value: dict[str, list[str]]) -> dict[str, list[str]]:
    return {key: list(items) for key, items in value.items()}


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


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
