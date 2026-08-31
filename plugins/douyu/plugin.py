from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from typing import Any
import urllib.parse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Conversation
from app.services.outbound_batch import enqueue_outbound_jobs_by_conversation
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient


DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DOUYU_XML_TEMPLATE = """
<appmsg appid="" sdkver="1"><title>{title}</title><des>{des}</des><type>5</type><url>{url}</url><thumburl>{thumburl}</thumburl></appmsg>
<scene>0</scene><appinfo><version>1</version><appname></appname></appinfo><commenturl></commenturl>
""".strip()


@dataclass(frozen=True)
class DouyuConfig:
    api_url_template: str
    yuba_api_url: str
    default_timeout: int
    default_feed_limit: int
    user_agent: str
    room_subscriptions: dict[str, list[str]]
    yuba_subscriptions: dict[str, list[str]]
    reminder_subscribers: dict[str, list[str]]
    room_states: dict[str, dict[str, Any]]
    yuba_last_feed_ids: dict[str, str]
    push_all_channels: bool
    poll_interval_seconds: int


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="douyu runtime is ready", output={"push_all_channels": config.push_all_channels, "poll_interval_seconds": config.poll_interval_seconds})
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")
    operation = _resolve_operation(request.payload)
    try:
        if operation == "subscribe_plan":
            return _update_plan(request.payload, config.room_subscriptions, "room_subscriptions", _resolve_room_id(request.payload), True, "已订阅斗鱼房间")
        if operation == "unsubscribe_plan":
            return _update_plan(request.payload, config.room_subscriptions, "room_subscriptions", _resolve_room_id(request.payload), False, "已取消订阅斗鱼房间")
        if operation == "list_plan":
            return _list_plan(request.payload, config.room_subscriptions, "当前没有斗鱼订阅。", "当前斗鱼订阅房间：")
        if operation == "subscribe_yuba":
            return _update_plan(request.payload, config.yuba_subscriptions, "yuba_subscriptions", _resolve_yuba_id(request.payload), True, "已订阅鱼吧")
        if operation == "unsubscribe_yuba":
            return _update_plan(request.payload, config.yuba_subscriptions, "yuba_subscriptions", _resolve_yuba_id(request.payload), False, "已取消订阅鱼吧")
        if operation == "list_yuba":
            return _list_plan(request.payload, config.yuba_subscriptions, "当前没有鱼吧订阅。", "当前鱼吧订阅列表：")
        if operation == "subscribe_reminder":
            sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
            return _update_plan(request.payload, config.reminder_subscribers, "reminder_subscribers", sender_id, True, "已加入斗鱼订阅提醒")
        if operation == "unsubscribe_reminder":
            sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
            return _update_plan(request.payload, config.reminder_subscribers, "reminder_subscribers", sender_id, False, "已取消斗鱼订阅提醒")
        if operation in {"yuba_feed", "feed"}:
            output = _fetch_yuba_feed_output(_resolve_yuba_id(request.payload), config)
            return PluginExecutionResult(ok=True, status="completed", message="douyu yuba feed fetched", output=output)
        output = _fetch_room_status_output(_resolve_room_id(request.payload), config)
        return PluginExecutionResult(ok=True, status="completed", message="douyu room status fetched", output=output)
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"douyu failed: {exc}")


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在任务所属租户会话中检查订阅变化并创建通用出站任务。"""

    if request.action != "poll_subscriptions":
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的斗鱼定时动作: {request.action}",
        )
    if request.context is None:
        return PluginExecutionResult(ok=False, status="error", message="斗鱼定时任务缺少可信上下文。")
    binding = db.get(BotPlugin, int(request.context.binding_id))
    if (
        binding is None
        or int(binding.tenant_id) != int(request.context.tenant_id)
        or int(binding.bot_id) != int(request.context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
    ):
        return PluginExecutionResult(ok=False, status="error", message="斗鱼插件绑定或机器人未启用。")

    config = _load_config(dict(request.config))
    room_states = dict(config.room_states)
    yuba_last_ids = dict(config.yuba_last_feed_ids)
    events: list[dict[str, Any]] = []

    try:
        for room_id in _all_values(config.room_subscriptions):
            room = _fetch_room_status_output(room_id, config)
            previous = str((config.room_states.get(room_id) or {}).get("provider_status") or "").strip()
            current = str(room.get("provider_status") or "").strip()
            if (previous == "live") != (current == "live"):
                events.append(
                    {
                        "type": "room_live" if current == "live" else "room_offline",
                        "room": room,
                        "target_keys": _targets_for_value(config.room_subscriptions, room_id),
                    }
                )
            room_states[room_id] = {
                "provider_status": current,
                "nickname": room.get("nickname"),
                "room_name": room.get("room_name"),
                "updated_at": now_shanghai().isoformat(),
            }

        for hash_id in _all_values(config.yuba_subscriptions):
            feed = _fetch_yuba_feed_output(hash_id, config, limit=max(10, config.default_feed_limit))
            items = feed.get("items") or []
            latest = items[0] if isinstance(items, list) and items else {}
            latest_id = str((latest or {}).get("feed_id") or "").strip()
            previous_id = str(config.yuba_last_feed_ids.get(hash_id) or "").strip()
            if latest_id and latest_id != previous_id:
                events.append(
                    {
                        "type": "yuba_update",
                        "item": latest,
                        "target_keys": _targets_for_value(config.yuba_subscriptions, hash_id),
                    }
                )
                yuba_last_ids[hash_id] = latest_id
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"斗鱼订阅检查失败: {exc}")

    conversations = _resolve_target_conversations(db, binding.bot_id, config, events)
    job_ids: list[int] = []
    failed_targets: list[dict[str, Any]] = []
    pushed_conversation_count = 0
    for event in events:
        batch_result = enqueue_outbound_jobs_by_conversation(
            db,
            conversations=conversations,
            plugin_code="douyu",
            related_type="douyu_binding",
            related_id=binding.id,
            priority=70,
            payload_builder=lambda conversation, channel, current_event=event: _build_event_payloads(
                conversation,
                current_event,
                config.reminder_subscribers,
            ),
            log_context={"binding_id": binding.id, "event_type": str(event.get("type") or "").strip()},
        )
        job_ids.extend(batch_result["job_ids"])
        failed_targets.extend(batch_result["failed_targets"])
        pushed_conversation_count += int(batch_result["pushed_conversation_count"] or 0)

    binding.config_json = json.dumps(
        _dump_config(config, room_states, yuba_last_ids),
        ensure_ascii=False,
        sort_keys=True,
    )
    summary = f"斗鱼已检查 {len(room_states)} 个房间和 {len(yuba_last_ids)} 个鱼吧订阅。"
    if pushed_conversation_count:
        summary += f" 已创建 {pushed_conversation_count} 个会话通知。"
    if failed_targets:
        summary += f" {len(failed_targets)} 个会话创建失败。"
    return PluginExecutionResult(
        ok=not failed_targets,
        status="completed" if not failed_targets else "partial",
        message=summary,
        output={
            "event_count": len(events),
            "outbound_job_ids": job_ids,
            "failed_targets": failed_targets,
        },
    )


def _fetch_room_status_output(room_id: str, config: DouyuConfig) -> dict[str, Any]:
    if not room_id:
        raise ValueError("room_id is required")
    body = _get_json(config.api_url_template.format(room_id=urllib.parse.quote(room_id)), timeout=config.default_timeout, user_agent=config.user_agent)
    room = body.get("room") if isinstance(body.get("room"), dict) else {}
    show_status = _coerce_int(room.get("show_status"), 0)
    video_loop = _coerce_int(room.get("videoLoop"), 0)
    avatar = room.get("avatar") if isinstance(room.get("avatar"), dict) else {}
    provider_status = "live" if show_status == 1 and video_loop == 0 else "looping" if video_loop == 1 else "offline"
    output = {
        "provider_status": provider_status,
        "room_id": room_id,
        "nickname": str(room.get("nickname") or "").strip(),
        "room_name": str(room.get("room_name") or "").strip(),
        "room_url": f"https://www.douyu.com/{room_id}",
        "thumburl": str(avatar.get("small") or room.get("room_src") or "").strip(),
    }
    if provider_status in {"live", "looping"}:
        output["douyu_xml"] = DOUYU_XML_TEMPLATE.format(title=escape(output["room_name"] or "Douyu Live"), des=escape(f"{output['nickname'] or 'unknown'} 正在直播"), url=escape(output["room_url"]), thumburl=escape(output["thumburl"]))
    output["reply_text"] = "\n".join(line for line in [f"{output['nickname']} - {output['room_name']}", f"状态: {provider_status}", output["room_url"]] if line.strip())
    output["replies"] = ([{"type": "xml", "xml": output["douyu_xml"]}] if output.get("douyu_xml") else [{"type": "text", "content": output["reply_text"]}])
    return output


def _fetch_yuba_feed_output(hash_id: str, config: DouyuConfig, *, limit: int | None = None) -> dict[str, Any]:
    if not hash_id:
        raise ValueError("hash_id is required")
    query = urllib.parse.urlencode({"filter_type": 1, "hash_id": hash_id, "limit": max(limit or config.default_feed_limit, 1), "offset": 0})
    body = _get_json(f"{config.yuba_api_url}?{query}", timeout=config.default_timeout, user_agent=config.user_agent)
    if int(body.get("error") or 0) != 0:
        raise ValueError(str(body.get("msg") or "yuba api returned error"))
    feed_list = ((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get("feed_list") or []
    items: list[dict[str, Any]] = []
    for raw in feed_list:
        if not isinstance(raw, dict) or int(raw.get("home_feed_top") or 0) == 1:
            continue
        publisher = raw.get("publisher") if isinstance(raw.get("publisher"), dict) else {}
        items.append({"feed_id": str(raw.get("feed_id") or "").strip(), "nickname": str(publisher.get("nickname") or "unknown").strip(), "text": str(raw.get("text") or "").strip(), "url": f"https://yuba.douyu.com/feed/{raw.get('feed_id')}"})
        if len(items) >= max(limit or config.default_feed_limit, 1):
            break
    text = "\n\n".join(f"{item['nickname']}: {item['text']}\n{item['url']}".strip() for item in items[:5]).strip()
    return {"provider_status": "fetched", "hash_id": hash_id, "count": len(items), "items": items, "reply_text": text, "replies": ([{"type": "text", "content": text}] if text else [])}


def _update_plan(payload: dict[str, object], mapping: dict[str, list[str]], field: str, value: str, add: bool, label: str) -> PluginExecutionResult:
    if not value:
        return PluginExecutionResult(ok=False, status="error", message=f"{field} value is required")
    target_key = _target_key(payload)
    updated = {key: list(items) for key, items in mapping.items()}
    current = updated.get(target_key, [])
    current = sorted({*current, value}) if add else [item for item in current if item != value]
    if current:
        updated[target_key] = current
    else:
        updated.pop(target_key, None)
    message = f"{label} {value}"
    return PluginExecutionResult(ok=True, status="completed", message=message, output={"provider_status": "planned", "config_patch": {field: updated}, "reply_text": message, "replies": [{"type": "text", "content": message}]})


def _list_plan(payload: dict[str, object], mapping: dict[str, list[str]], empty_text: str, title: str) -> PluginExecutionResult:
    items = mapping.get(_target_key(payload), [])
    text = empty_text if not items else title + "\n" + "\n".join(items)
    return PluginExecutionResult(ok=True, status="completed", message="subscription plan listed", output={"provider_status": "planned", "reply_text": text, "replies": [{"type": "text", "content": text}]})


def _resolve_operation(payload: dict[str, object]) -> str:
    explicit = str(payload.get("operation") or payload.get("mode") or "").strip().lower()
    if explicit:
        return explicit
    first = str(payload.get("content") or "").strip().split(" ")[0]
    return {
        "斗鱼订阅": "subscribe_plan",
        "取消斗鱼订阅": "unsubscribe_plan",
        "斗鱼订阅列表": "list_plan",
        "订阅鱼吧": "subscribe_yuba",
        "取消订阅鱼吧": "unsubscribe_yuba",
        "鱼吧订阅列表": "list_yuba",
        "斗鱼订阅提醒": "subscribe_reminder",
        "取消斗鱼订阅提醒": "unsubscribe_reminder",
    }.get(first, "room_status")


def _resolve_room_id(payload: dict[str, object]) -> str:
    return _resolve_second_arg(payload, "room_id")


def _resolve_yuba_id(payload: dict[str, object]) -> str:
    return _resolve_second_arg(payload, "hash_id")


def _resolve_second_arg(payload: dict[str, object], key: str) -> str:
    for item_key in (key, "keyword", "command_args"):
        value = str(payload.get(item_key) or "").strip()
        if value:
            return value
    parts = [item for item in str(payload.get("content") or "").strip().split() if item]
    return parts[1].strip() if len(parts) >= 2 else str(payload.get("query") or "").strip()


def _target_key(payload: dict[str, object]) -> str:
    return str(payload.get("conversation_external_id") or payload.get("external_id") or payload.get("conversation_id") or "global").strip() or "global"


def _all_values(mapping: dict[str, list[str]]) -> list[str]:
    return sorted({str(item).strip() for items in mapping.values() for item in items if str(item).strip()})


def _targets_for_value(mapping: dict[str, list[str]], value: str) -> list[str]:
    return sorted([key for key, items in mapping.items() if value in items])


def _resolve_target_conversations(db, bot_id: int, config: DouyuConfig, events: list[dict[str, Any]]) -> list[Conversation]:
    keys = {key for event in events for key in event.get("target_keys") or [] if str(key).strip()}
    if config.push_all_channels:
        keys.update(config.room_subscriptions.keys())
        keys.update(config.yuba_subscriptions.keys())
        keys.update(config.reminder_subscribers.keys())
    if not keys:
        return []
    return db.scalars(select(Conversation).where(Conversation.bot_id == bot_id, Conversation.chat_type == "group", Conversation.external_id.in_(sorted(keys)))).all()


def _build_event_payloads(conversation: Conversation, event: dict[str, Any], reminders_map: dict[str, list[str]]) -> list[dict[str, Any]]:
    reminders = list(reminders_map.get(str(conversation.external_id), []))
    payloads: list[dict[str, Any]] = []
    if event["type"] == "room_live":
        room = event["room"]
        lines = ["斗鱼开播提醒", f"{room.get('nickname') or '未知主播'} 开播了", f"标题：{room.get('room_name') or '斗鱼直播'}"]
        if reminders:
            lines.append(f"提醒对象：{' '.join(reminders)}")
        lines.append(str(room.get("room_url") or ""))
        payloads.append({"external_id": conversation.external_id, "receiver_id": conversation.external_id, "chat_type": conversation.chat_type, "message_type": "text", "content": "\n".join(line for line in lines if line.strip())})
    elif event["type"] == "room_offline":
        room = event["room"]
        lines = ["斗鱼下播提醒", f"{room.get('nickname') or '未知主播'} 已下播", f"标题：{room.get('room_name') or '斗鱼直播'}"]
        if reminders:
            lines.append(f"提醒对象：{' '.join(reminders)}")
        payloads.append({"external_id": conversation.external_id, "receiver_id": conversation.external_id, "chat_type": conversation.chat_type, "message_type": "text", "content": "\n".join(lines)})
    else:
        item = event["item"]
        lines = ["鱼吧更新提醒", f"主播：{item.get('nickname') or '未知主播'}", f"内容：{item.get('text') or ''}"]
        if reminders:
            lines.append(f"提醒对象：{' '.join(reminders)}")
        lines.append(str(item.get("url") or ""))
        payloads.append({"external_id": conversation.external_id, "receiver_id": conversation.external_id, "chat_type": conversation.chat_type, "message_type": "text", "content": "\n".join(line for line in lines if line.strip())})
    return payloads


def _get_json(url: str, *, timeout: int, user_agent: str) -> dict[str, object]:
    response = PluginHttpClient().request(
        "GET",
        url,
        headers={"Accept": "application/json", "User-Agent": user_agent},
        connect_timeout_seconds=max(1, timeout),
        total_timeout_seconds=max(1, timeout),
        max_response_bytes=2 * 1024 * 1024,
        retry_count=1,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"provider returned HTTP {response.status_code}")
    parsed = response.json()
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")


def _load_config(raw: dict[str, object]) -> DouyuConfig:
    return DouyuConfig(api_url_template=str(raw.get("api_url_template") or "https://www.douyu.com/betard/{room_id}").strip(), yuba_api_url=str(raw.get("yuba_api_url") or "https://yuba.douyu.com/wgapi/yubanc/api/feed/getUserFeedList").strip(), default_timeout=_coerce_int(raw.get("default_timeout"), 10), default_feed_limit=_coerce_int(raw.get("default_feed_limit"), 3), user_agent=str(raw.get("user_agent") or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT, room_subscriptions=_normalize_mapping(raw.get("room_subscriptions")), yuba_subscriptions=_normalize_mapping(raw.get("yuba_subscriptions")), reminder_subscribers=_normalize_mapping(raw.get("reminder_subscribers")), room_states=_normalize_state_mapping(raw.get("room_states")), yuba_last_feed_ids=_normalize_scalar_mapping(raw.get("yuba_last_feed_ids")), push_all_channels=bool(raw.get("push_all_channels", True)), poll_interval_seconds=max(30, _coerce_int(raw.get("poll_interval_seconds"), 60)))


def _dump_config(config: DouyuConfig, room_states: dict[str, dict[str, Any]], yuba_last_feed_ids: dict[str, str]) -> dict[str, Any]:
    return {"api_url_template": config.api_url_template, "yuba_api_url": config.yuba_api_url, "default_timeout": config.default_timeout, "default_feed_limit": config.default_feed_limit, "user_agent": config.user_agent, "room_subscriptions": config.room_subscriptions, "yuba_subscriptions": config.yuba_subscriptions, "reminder_subscribers": config.reminder_subscribers, "room_states": room_states, "yuba_last_feed_ids": yuba_last_feed_ids, "push_all_channels": config.push_all_channels, "poll_interval_seconds": config.poll_interval_seconds}


def _normalize_mapping(value: object) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): sorted({str(item).strip() for item in items if str(item).strip()}) for key, items in value.items() if str(key).strip() and isinstance(items, list)}


def _normalize_state_mapping(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): dict(item) for key, item in value.items() if str(key).strip() and isinstance(item, dict)}


def _normalize_scalar_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip(): str(item).strip() for key, item in value.items() if str(key).strip() and str(item).strip()}


def _coerce_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
