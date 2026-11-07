from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import BotPlugin, Channel, Conversation
from app.services.outbound_batch import enqueue_outbound_jobs
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore


DEFAULT_ROOM_API_URL_TEMPLATE = "https://www.douyu.com/betard/{room_id}"
DEFAULT_YUBA_API_URL = "https://yuba.douyu.com/wgapi/yubanc/api/feed/getUserFeedList"
DEFAULT_HIGHLIGHT_API_URL = "https://www.douyu.com/wgapi/vodnc/center/ailive/getHighlightDetail"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

ROOM_STATE_NAMESPACE = "douyu_room"
YUBA_STATE_NAMESPACE = "douyu_yuba"
HIGHLIGHT_ROOM_STATE_NAMESPACE = "douyu_highlight_room"
HIGHLIGHT_DELIVERY_STATE_NAMESPACE = "douyu_highlight_delivery"

ROOM_ID_PATTERN = re.compile(r"^[0-9]{1,20}$")
YUBA_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
MUTATING_OPERATIONS = {
    "subscribe_room",
    "unsubscribe_room",
    "list_rooms",
    "subscribe_yuba",
    "unsubscribe_yuba",
    "list_yubas",
    "subscribe_reminder",
    "unsubscribe_reminder",
    "enable_highlights",
    "disable_highlights",
    "highlight_status",
}


@dataclass(frozen=True)
class DouyuConfig:
    """斗鱼插件的租户配置；订阅定义和运行状态严格分离。"""

    api_url_template: str
    yuba_api_url: str
    highlight_api_url: str
    timeout_seconds: int
    feed_limit: int
    user_agent: str
    admin_user_ids: tuple[str, ...]
    room_subscriptions: dict[str, list[str]]
    yuba_subscriptions: dict[str, list[str]]
    reminder_subscribers: dict[str, list[str]]
    highlight_push_conversation_ids: tuple[str, ...]
    highlight_enabled: bool
    highlight_interval_seconds: int
    highlight_retention_days: int
    highlight_realtime_max_items: int
    poll_interval_seconds: int


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """处理无数据库依赖的房间和鱼吧查询；订阅操作必须进入可信消息入口。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="斗鱼插件已就绪。",
            output={
                "highlight_enabled": config.highlight_enabled,
                "poll_interval_seconds": config.poll_interval_seconds,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    operation = _resolve_operation(request.payload)
    if operation in MUTATING_OPERATIONS:
        return _error_result("斗鱼订阅只能通过真实群聊会话操作。", "trusted_context_required")

    try:
        if operation == "yuba_feed":
            hash_id = _resolve_yuba_id(request.payload)
            items = _fetch_yuba_feed(hash_id, config)
            reply_text = _build_yuba_query_text(hash_id, items)
            return PluginExecutionResult(
                ok=True,
                status="completed",
                message=f"已返回 {len(items)} 条鱼吧动态。",
                output={
                    "provider_status": "completed",
                    "hash_id": hash_id,
                    "result_count": len(items),
                    # 执行记录只保存最终业务文本，不复制第三方完整列表和扩展字段。
                    "replies": standard_replies([text_reply(reply_text)]),
                },
            )
        output = _fetch_room_status_output(_resolve_room_id(request.payload), config)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="已返回斗鱼房间状态。",
            output=output,
        )
    except (PluginHttpError, ValueError, json.JSONDecodeError):
        return _error_result("斗鱼查询暂时不可用，请稍后重试。", "fetch_failed")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在租户数据库会话内处理订阅命令，身份只读取平台构造的可信上下文。"""

    if request.action != "execute":
        return execute_action(request)
    operation = _resolve_operation(request.payload)
    if operation not in MUTATING_OPERATIONS:
        return execute_action(request)

    context, conversation, error = _require_group_context(db, request.context)
    if error is not None or context is None or conversation is None:
        return error or _error_result("斗鱼命令缺少可信群聊上下文。", "trusted_context_required")

    config = _load_config(request.config)
    conversation_key = str(conversation.id)
    store = PluginStateStore(db, context)

    if operation == "subscribe_room":
        room_id = _resolve_room_id(request.payload)
        if not ROOM_ID_PATTERN.fullmatch(room_id):
            return _error_result("房间号必须是数字。", "invalid_room_id")
        result = _update_mapping_result(
            config.room_subscriptions,
            field_name="room_subscriptions",
            conversation_key=conversation_key,
            value=room_id,
            add=True,
            message=f"已订阅斗鱼房间 {room_id}。",
        )
        # 当前群已经开启高能推送时，新订阅房间先继承平台已见基线，避免补发
        # 订阅前产生的内容。该基线只包含稳定 ID，不保存第三方完整响应。
        if conversation_key in config.highlight_push_conversation_ids:
            _seed_highlight_delivery_baseline(store, conversation_key, [room_id], config)
        return result

    if operation == "unsubscribe_room":
        room_id = _resolve_room_id(request.payload)
        if not ROOM_ID_PATTERN.fullmatch(room_id):
            return _error_result("房间号必须是数字。", "invalid_room_id")
        store.delete(HIGHLIGHT_DELIVERY_STATE_NAMESPACE, _delivery_state_key(conversation_key, room_id))
        return _update_mapping_result(
            config.room_subscriptions,
            field_name="room_subscriptions",
            conversation_key=conversation_key,
            value=room_id,
            add=False,
            message=f"已取消订阅斗鱼房间 {room_id}。",
        )

    if operation == "list_rooms":
        return _list_mapping_result(
            config.room_subscriptions,
            conversation_key,
            empty_text="当前群还没有斗鱼房间订阅。",
            title="当前群订阅的斗鱼房间：",
        )

    if operation == "subscribe_yuba":
        hash_id = _resolve_yuba_id(request.payload)
        if not YUBA_ID_PATTERN.fullmatch(hash_id):
            return _error_result("鱼吧标识格式不正确。", "invalid_yuba_id")
        return _update_mapping_result(
            config.yuba_subscriptions,
            field_name="yuba_subscriptions",
            conversation_key=conversation_key,
            value=hash_id,
            add=True,
            message=f"已订阅斗鱼鱼吧 {hash_id}。",
        )

    if operation == "unsubscribe_yuba":
        hash_id = _resolve_yuba_id(request.payload)
        if not YUBA_ID_PATTERN.fullmatch(hash_id):
            return _error_result("鱼吧标识格式不正确。", "invalid_yuba_id")
        return _update_mapping_result(
            config.yuba_subscriptions,
            field_name="yuba_subscriptions",
            conversation_key=conversation_key,
            value=hash_id,
            add=False,
            message=f"已取消订阅斗鱼鱼吧 {hash_id}。",
        )

    if operation == "list_yubas":
        return _list_mapping_result(
            config.yuba_subscriptions,
            conversation_key,
            empty_text="当前群还没有鱼吧订阅。",
            title="当前群订阅的斗鱼鱼吧：",
        )

    if operation in {"subscribe_reminder", "unsubscribe_reminder"}:
        sender_id = str(context.sender_id or "").strip()
        if not sender_id:
            return _error_result("无法识别当前成员。", "sender_required")
        enabled = operation == "subscribe_reminder"
        return _update_mapping_result(
            config.reminder_subscribers,
            field_name="reminder_subscribers",
            conversation_key=conversation_key,
            value=sender_id,
            add=enabled,
            message="已加入斗鱼提醒名单。" if enabled else "已退出斗鱼提醒名单。",
        )

    if operation == "highlight_status":
        enabled = conversation_key in config.highlight_push_conversation_ids
        text = "当前群已开启斗鱼高能推送。" if enabled else "当前群未开启斗鱼高能推送。"
        return _text_result(text, provider_status="enabled" if enabled else "disabled")

    if str(context.sender_id or "").strip() not in config.admin_user_ids:
        return PluginExecutionResult(
            ok=False,
            status="forbidden",
            message="只有插件配置中的管理员可以修改高能推送。",
            output={
                "provider_status": "forbidden",
                "replies": standard_replies([text_reply("只有管理员可以修改斗鱼高能推送。")]),
            },
        )

    enabled = operation == "enable_highlights"
    updated_ids = set(config.highlight_push_conversation_ids)
    if enabled:
        updated_ids.add(conversation_key)
        _seed_highlight_delivery_baseline(
            store,
            conversation_key,
            config.room_subscriptions.get(conversation_key, []),
            config,
        )
    else:
        updated_ids.discard(conversation_key)
    text = "已开启当前群的斗鱼高能推送。" if enabled else "已关闭当前群的斗鱼高能推送。"
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text,
        output={
            "provider_status": "configured",
            "config_patch": {"highlight_push_conversation_ids": sorted(updated_ids, key=int)},
            "replies": standard_replies([text_reply(text)]),
        },
    )


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """通过统一调度器检查直播、鱼吧和官方高能，并按精确会话创建出站任务。"""

    if request.action != "poll_subscriptions":
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的定时动作: {request.action}",
        )
    context = request.context
    if context is None:
        return _error_result("斗鱼定时任务缺少可信绑定上下文。", "trusted_context_required")
    binding = _load_active_binding(db, context)
    if binding is None:
        return _error_result("斗鱼插件绑定或机器人未启用。", "binding_unavailable")

    config = _load_config(request.config)
    store = PluginStateStore(db, context)
    current = now_shanghai()
    events: list[dict[str, Any]] = []
    failed_checks = 0

    for room_id in _all_mapping_values(config.room_subscriptions):
        try:
            room = _fetch_room_status_output(room_id, config)
            previous = store.get(ROOM_STATE_NAMESPACE, room_id) or {}
            previous_status = str(previous.get("provider_status") or "").strip()
            current_status = str(room.get("provider_status") or "").strip()
            previous_live = previous_status == "live"
            current_live = current_status == "live"

            # 第一次观察到直播中的房间需要发送开播提醒；第一次观察到离线房间只建立
            # 基线。后续仅在直播布尔状态变化时通知，轮播不会被误判为正式开播。
            if (not previous_status and current_live) or (previous_status and previous_live != current_live):
                events.append(
                    {
                        "type": "room_live" if current_live else "room_offline",
                        "room": room,
                        "target_conversation_ids": _targets_for_value(config.room_subscriptions, room_id),
                    }
                )
            store.put(
                ROOM_STATE_NAMESPACE,
                room_id,
                {
                    "provider_status": current_status,
                    "nickname": str(room.get("nickname") or ""),
                    "room_name": str(room.get("room_name") or ""),
                    "updated_at": current.isoformat(),
                },
            )

            should_fetch_highlights = config.highlight_enabled and (
                current_live or (previous_live and not current_live)
            )
            if should_fetch_highlights:
                highlight_event = _poll_room_highlights(
                    store,
                    config,
                    room_id=room_id,
                    nickname=str(room.get("nickname") or ""),
                    force=previous_live and not current_live,
                    current=current,
                )
                if highlight_event is not None:
                    events.append(highlight_event)
        except (PluginHttpError, ValueError, json.JSONDecodeError):
            # 单个房间失败不能阻断同一租户的其他订阅。失败数量只进入运行摘要，
            # 不保存第三方错误页、URL 参数或响应体。
            failed_checks += 1

    for hash_id in _all_mapping_values(config.yuba_subscriptions):
        try:
            items = _fetch_yuba_feed(hash_id, config, limit=max(config.feed_limit, 10))
            latest = items[0] if items else None
            latest_id = str((latest or {}).get("feed_id") or "").strip()
            previous = store.get(YUBA_STATE_NAMESPACE, hash_id) or {}
            previous_id = str(previous.get("latest_feed_id") or "").strip()
            if latest is not None and latest_id and latest_id != previous_id:
                events.append(
                    {
                        "type": "yuba_update",
                        "item": latest,
                        "target_conversation_ids": _targets_for_value(config.yuba_subscriptions, hash_id),
                    }
                )
                store.put(
                    YUBA_STATE_NAMESPACE,
                    hash_id,
                    {"latest_feed_id": latest_id, "updated_at": current.isoformat()},
                )
        except (PluginHttpError, ValueError, json.JSONDecodeError):
            failed_checks += 1

    job_ids: list[int] = []
    failed_targets: list[dict[str, Any]] = []
    pushed_conversation_count = 0
    for event in events:
        if event["type"] == "highlight_update":
            target_items = event.get("target_items") or {}
            for conversation_key, items in target_items.items():
                result = _enqueue_event_for_conversation(
                    db,
                    binding=binding,
                    context=context,
                    conversation_id=_positive_int(conversation_key),
                    content=_build_highlight_message(
                        str(event.get("room_id") or ""),
                        str(event.get("nickname") or ""),
                        list(items),
                        limit=config.highlight_realtime_max_items,
                    ),
                    event_type="highlight_update",
                )
                job_ids.extend(result["job_ids"])
                if result["error"]:
                    failed_targets.append(result["error"])
                    continue
                pushed_conversation_count += 1
                _mark_highlights_delivered(
                    store,
                    str(conversation_key),
                    str(event.get("room_id") or ""),
                    [str(item.get("global_highlight_id") or "") for item in items],
                    config,
                )
            continue

        for conversation_key in event.get("target_conversation_ids") or []:
            content = _build_event_text(event, config.reminder_subscribers, str(conversation_key))
            result = _enqueue_event_for_conversation(
                db,
                binding=binding,
                context=context,
                conversation_id=_positive_int(conversation_key),
                content=content,
                event_type=str(event.get("type") or "douyu_update"),
            )
            job_ids.extend(result["job_ids"])
            if result["error"]:
                failed_targets.append(result["error"])
            else:
                pushed_conversation_count += 1

    db.flush()
    status = "partial" if failed_checks or failed_targets else "completed"
    summary = (
        f"斗鱼已检查 {len(_all_mapping_values(config.room_subscriptions))} 个房间和 "
        f"{len(_all_mapping_values(config.yuba_subscriptions))} 个鱼吧订阅，"
        f"创建 {pushed_conversation_count} 个会话通知。"
    )
    if failed_checks or failed_targets:
        summary += f" 检查失败 {failed_checks} 项，投递失败 {len(failed_targets)} 项。"
    return PluginExecutionResult(
        ok=not failed_targets,
        status=status,
        message=summary,
        output={
            "provider_status": status,
            "event_count": len(events),
            "outbound_job_ids": job_ids,
            "failed_check_count": failed_checks,
            "failed_targets": failed_targets,
        },
    )


def _fetch_room_status_output(room_id: str, config: DouyuConfig) -> dict[str, Any]:
    """读取官方房间页 JSON，只保留状态展示和通知真正需要的字段。"""

    if not ROOM_ID_PATTERN.fullmatch(room_id):
        raise ValueError("invalid room id")
    url = config.api_url_template.format(room_id=quote(room_id, safe=""))
    body = _request_json("GET", url, config=config, referer=f"https://www.douyu.com/{room_id}")
    room = body.get("room") if isinstance(body.get("room"), Mapping) else {}
    show_status = _coerce_int(room.get("show_status"), 0)
    video_loop = _coerce_int(room.get("videoLoop"), 0)
    provider_status = "live" if show_status == 1 and video_loop == 0 else "looping" if video_loop == 1 else "offline"
    nickname = _clean_text(room.get("nickname"), 80)
    room_name = _clean_text(room.get("room_name"), 160)
    room_url = f"https://www.douyu.com/{room_id}"
    status_label = {"live": "直播中", "looping": "轮播中", "offline": "未开播"}[provider_status]
    reply_text = "\n".join(
        line
        for line in (
            f"斗鱼房间 {room_id}",
            f"主播：{nickname or '未知主播'}",
            f"标题：{room_name or '-'}",
            f"状态：{status_label}",
            room_url,
        )
        if line.strip()
    )
    return {
        "provider_status": provider_status,
        "room_id": room_id,
        "nickname": nickname,
        "room_name": room_name,
        "room_url": room_url,
        "replies": standard_replies([text_reply(reply_text)]),
    }


def _fetch_yuba_feed(hash_id: str, config: DouyuConfig, *, limit: int | None = None) -> list[dict[str, Any]]:
    """读取鱼吧非置顶动态，并把上游对象压缩为投递所需的小型结构。"""

    if not YUBA_ID_PATTERN.fullmatch(hash_id):
        raise ValueError("invalid yuba id")
    bounded_limit = max(1, min(int(limit or config.feed_limit), 20))
    query = urlencode({"filter_type": 1, "hash_id": hash_id, "limit": bounded_limit, "offset": 0})
    body = _request_json(
        "GET",
        f"{config.yuba_api_url}?{query}",
        config=config,
        referer=f"https://yuba.douyu.com/member/{quote(hash_id, safe='')}/main/news",
    )
    if _coerce_int(body.get("error"), -1) != 0:
        raise ValueError("yuba provider error")
    data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
    raw_items = data.get("feed_list") if isinstance(data.get("feed_list"), list) else []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping) or _coerce_int(raw.get("home_feed_top"), 0) == 1:
            continue
        feed_id = _clean_text(raw.get("feed_id"), 120)
        if not feed_id:
            continue
        publisher = raw.get("publisher") if isinstance(raw.get("publisher"), Mapping) else {}
        items.append(
            {
                "feed_id": feed_id,
                "nickname": _clean_text(publisher.get("nickname"), 80) or "未知主播",
                "text": _clean_text(raw.get("text"), 220),
                "url": f"https://yuba.douyu.com/feed/{quote(feed_id, safe='')}",
            }
        )
        if len(items) >= bounded_limit:
            break
    return items


def _fetch_highlights(room_id: str, config: DouyuConfig) -> list[dict[str, Any]]:
    """读取斗鱼官方高能接口，缺少稳定全局 ID 的记录不进入去重链路。"""

    if not ROOM_ID_PATTERN.fullmatch(room_id):
        raise ValueError("invalid room id")
    body = _request_json(
        "POST",
        config.highlight_api_url,
        config=config,
        referer=(
            "https://www.douyu.com/pages/ai-live-summary"
            f"?rid={room_id}&isAnchorSide=0&sourcekey=ai-live-summary"
        ),
        json_body={"rid": int(room_id)},
    )
    if _coerce_int(body.get("error"), -1) != 0:
        raise ValueError("highlight provider error")
    data = body.get("data") if isinstance(body.get("data"), Mapping) else {}
    raw_items = data.get("highlightList") if isinstance(data.get("highlightList"), list) else []
    normalized_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        global_id = _clean_text(raw.get("globalHighlightId"), 120)
        if not global_id:
            continue
        normalized_by_id[global_id] = {
            "global_highlight_id": global_id,
            "highlight_id": _clean_text(raw.get("highlightId"), 120),
            "title": _clean_text(raw.get("title"), 100) or "直播高能时刻",
            "description": _clean_text(raw.get("describe") or raw.get("description"), 500),
            "heat": max(0, _coerce_int(raw.get("heat"), 0)),
            "start_time": _clean_text(raw.get("startTime"), 40),
        }
    return sorted(
        normalized_by_id.values(),
        key=lambda item: (str(item.get("start_time") or ""), str(item.get("global_highlight_id") or "")),
    )


def _poll_room_highlights(
    store: PluginStateStore,
    config: DouyuConfig,
    *,
    room_id: str,
    nickname: str,
    force: bool,
    current: datetime,
) -> dict[str, Any] | None:
    """按房间节流采集高能，并为每个启用会话计算独立的待投递集合。"""

    state = store.get(HIGHLIGHT_ROOM_STATE_NAMESPACE, room_id) or {}
    last_fetch_at = str(state.get("last_fetch_at") or "").strip()
    if not force and last_fetch_at and not _interval_elapsed(last_fetch_at, current, config.highlight_interval_seconds):
        return None

    ttl_seconds = config.highlight_retention_days * 24 * 3600
    # 请求前先记录尝试时间。上游临时失败时仍执行节流，避免统一调度器在每个扫描
    # 周期重复冲击公开网页接口；已知 ID 保持不变，不会因此丢失去重基线。
    store.put(
        HIGHLIGHT_ROOM_STATE_NAMESPACE,
        room_id,
        {
            "initialized": bool(state.get("initialized")),
            "known_ids": _normalize_text_list(state.get("known_ids"), maximum=200),
            "last_fetch_at": current.isoformat(),
        },
        ttl_seconds=ttl_seconds,
    )
    highlights = _fetch_highlights(room_id, config)
    current_ids = sorted(
        {
            str(item.get("global_highlight_id") or "").strip()
            for item in highlights
            if str(item.get("global_highlight_id") or "").strip()
        }
    )[-200:]
    initialized = bool(state.get("initialized"))
    store.put(
        HIGHLIGHT_ROOM_STATE_NAMESPACE,
        room_id,
        {"initialized": True, "known_ids": current_ids, "last_fetch_at": current.isoformat()},
        ttl_seconds=ttl_seconds,
    )

    target_ids = sorted(
        set(_targets_for_value(config.room_subscriptions, room_id))
        & set(config.highlight_push_conversation_ids),
        key=int,
    )
    if not initialized:
        # 插件第一次看到一个房间时只建立静默基线。所有已经开启推送的会话都把
        # 当前列表标记为已投递，只接收后续新增内容。
        for conversation_key in target_ids:
            _put_delivery_ids(store, conversation_key, room_id, current_ids, config)
        return None

    target_items: dict[str, list[dict[str, Any]]] = {}
    for conversation_key in target_ids:
        delivery_key = _delivery_state_key(conversation_key, room_id)
        delivery_state = store.get(HIGHLIGHT_DELIVERY_STATE_NAMESPACE, delivery_key)
        if delivery_state is None:
            # 管理后台直接打开开关时不会经过聊天命令的基线初始化。这里仍然静默
            # 建立一次会话基线，避免把订阅前的历史高能作为新消息补发。
            _put_delivery_ids(store, conversation_key, room_id, current_ids, config)
            continue
        delivered_ids = set(_normalize_text_list(delivery_state.get("delivered_ids"), maximum=200))
        pending = [
            item
            for item in highlights
            if str(item.get("global_highlight_id") or "").strip() not in delivered_ids
        ]
        if pending:
            target_items[conversation_key] = pending

    if not target_items:
        return None
    return {
        "type": "highlight_update",
        "room_id": room_id,
        "nickname": nickname,
        "target_items": target_items,
    }


def _seed_highlight_delivery_baseline(
    store: PluginStateStore,
    conversation_key: str,
    room_ids: list[str],
    config: DouyuConfig,
) -> None:
    """开启会话推送或新增房间时，把平台已见高能复制为该会话的投递基线。"""

    for room_id in sorted({str(item) for item in room_ids if ROOM_ID_PATTERN.fullmatch(str(item))}):
        room_state = store.get(HIGHLIGHT_ROOM_STATE_NAMESPACE, room_id) or {}
        known_ids = _normalize_text_list(room_state.get("known_ids"), maximum=200)
        _put_delivery_ids(store, conversation_key, room_id, known_ids, config)


def _put_delivery_ids(
    store: PluginStateStore,
    conversation_key: str,
    room_id: str,
    delivered_ids: list[str],
    config: DouyuConfig,
) -> None:
    """保存会话级高能投递 ID，并通过 TTL 和数量上限控制状态体积。"""

    store.put(
        HIGHLIGHT_DELIVERY_STATE_NAMESPACE,
        _delivery_state_key(conversation_key, room_id),
        {"delivered_ids": sorted(set(_normalize_text_list(delivered_ids, maximum=200)))},
        ttl_seconds=config.highlight_retention_days * 24 * 3600,
    )


def _mark_highlights_delivered(
    store: PluginStateStore,
    conversation_key: str,
    room_id: str,
    delivered_ids: list[str],
    config: DouyuConfig,
) -> None:
    """只有出站任务成功创建后才合并投递 ID，失败会话可在下一轮继续尝试。"""

    key = _delivery_state_key(conversation_key, room_id)
    current = store.get(HIGHLIGHT_DELIVERY_STATE_NAMESPACE, key) or {}
    merged = set(_normalize_text_list(current.get("delivered_ids"), maximum=200))
    merged.update(_normalize_text_list(delivered_ids, maximum=200))
    _put_delivery_ids(store, conversation_key, room_id, sorted(merged)[-200:], config)


def _enqueue_event_for_conversation(
    db: Session,
    *,
    binding: BotPlugin,
    context: PluginExecutionContext,
    conversation_id: int | None,
    content: str,
    event_type: str,
) -> dict[str, Any]:
    """校验精确租户、机器人和渠道后，为一个会话创建通用文本出站任务。"""

    conversation = db.get(Conversation, int(conversation_id or 0))
    channel = db.get(Channel, int(conversation.channel_id or 0)) if conversation is not None else None
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or conversation.chat_type != "group"
        or channel is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id or 0) != int(context.bot_id)
        or not channel.is_enabled
    ):
        return {
            "job_ids": [],
            "error": {"conversation_id": int(conversation_id or 0), "error": "conversation_unavailable"},
        }

    payload = {
        "external_id": conversation.external_id,
        "receiver_id": conversation.external_id,
        "chat_type": conversation.chat_type,
        "message_type": "text",
        "content": content,
    }
    try:
        # 保存点隔离单个会话的渠道异常，避免一个失效渠道回滚同一轮其他会话已经
        # 建立的任务和状态。外层租户事务仍统一决定最终提交时机。
        with db.begin_nested():
            job_ids = enqueue_outbound_jobs(
                db,
                conversation=conversation,
                channel=channel,
                payloads=[payload],
                plugin_code="douyu",
                related_type="douyu_binding",
                related_id=int(binding.id),
                priority=70,
            )
        return {"job_ids": job_ids, "error": None}
    except Exception as exc:
        return {
            "job_ids": [],
            "error": {
                "conversation_id": int(conversation.id),
                "channel_id": int(channel.id),
                "event_type": event_type,
                "error": str(exc)[:160],
            },
        }


def _build_event_text(
    event: Mapping[str, Any],
    reminders_map: dict[str, list[str]],
    conversation_key: str,
) -> str:
    """把直播或鱼吧事件转换为不依赖渠道协议的业务文本。"""

    event_type = str(event.get("type") or "")
    reminders = reminders_map.get(conversation_key, [])
    if event_type in {"room_live", "room_offline"}:
        room = event.get("room") if isinstance(event.get("room"), Mapping) else {}
        lines = [
            "斗鱼开播提醒" if event_type == "room_live" else "斗鱼下播提醒",
            f"主播：{room.get('nickname') or '未知主播'}",
            f"标题：{room.get('room_name') or '-'}",
        ]
        if reminders:
            lines.append("提醒成员：" + " ".join(reminders))
        if event_type == "room_live" and room.get("room_url"):
            lines.append(str(room["room_url"]))
        return "\n".join(lines)

    item = event.get("item") if isinstance(event.get("item"), Mapping) else {}
    lines = [
        "鱼吧更新提醒",
        f"主播：{item.get('nickname') or '未知主播'}",
        f"内容：{item.get('text') or '-'}",
    ]
    if reminders:
        lines.append("提醒成员：" + " ".join(reminders))
    if item.get("url"):
        lines.append(str(item["url"]))
    return "\n".join(lines)


def _build_highlight_message(room_id: str, nickname: str, items: list[dict[str, Any]], *, limit: int) -> str:
    """把一轮新增官方高能合并成单条文本，避免同群短时间连续刷屏。"""

    ordered = sorted(
        items,
        key=lambda item: (max(0, _coerce_int(item.get("heat"), 0)), str(item.get("global_highlight_id") or "")),
        reverse=True,
    )
    selected = ordered[: max(1, min(limit, 10))]
    lines = [f"斗鱼官方高能 | {nickname or f'房间 {room_id}'}", f"房间号：{room_id}"]
    for index, item in enumerate(selected, start=1):
        lines.extend(["", f"{index}. {item.get('title') or '直播高能时刻'}"])
        if _coerce_int(item.get("heat"), 0) > 0:
            lines.append(f"热度：{_coerce_int(item.get('heat'), 0)}")
        if item.get("description"):
            lines.append(f"内容：{item['description']}")
        global_id = str(item.get("global_highlight_id") or "").strip()
        if global_id:
            lines.append(
                "高能详情："
                f"https://www.douyu.com/pages/ai-live-summary/redirect?ghid={quote(global_id, safe='')}"
            )
    lines.extend(["", f"打开直播间：https://www.douyu.com/{quote(room_id, safe='')}"])
    return "\n".join(lines).strip()


def _build_yuba_query_text(hash_id: str, items: list[dict[str, Any]]) -> str:
    """生成鱼吧查询文本；没有动态时仍返回明确业务结果。"""

    if not items:
        return f"鱼吧 {hash_id} 暂无可展示动态。"
    lines = [f"鱼吧 {hash_id} 最新动态"]
    for index, item in enumerate(items[:5], start=1):
        lines.extend(
            [
                "",
                f"{index}. {item.get('nickname') or '未知主播'}",
                str(item.get("text") or "-"),
                str(item.get("url") or ""),
            ]
        )
    return "\n".join(line for line in lines if line).strip()


def _request_json(
    method: str,
    url: str,
    *,
    config: DouyuConfig,
    referer: str,
    json_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """通过平台受控客户端请求 JSON，统一限制超时、重定向和响应体大小。"""

    response = PluginHttpClient().request(
        method,
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.douyu.com",
            "Referer": referer,
            "User-Agent": config.user_agent,
        },
        json_body=dict(json_body) if json_body is not None else None,
        connect_timeout_seconds=min(config.timeout_seconds, 8),
        total_timeout_seconds=config.timeout_seconds,
        max_response_bytes=2 * 1024 * 1024,
        max_redirects=2,
        retry_count=1,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"douyu http {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("douyu response must be object")
    return payload


def _require_group_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[PluginExecutionContext | None, Conversation | None, PluginExecutionResult | None]:
    """验证可信上下文对应当前租户、机器人和群会话，完全忽略 payload 同名字段。"""

    if context is None or context.conversation_id is None or context.chat_type != "group":
        return None, None, _error_result("请在群聊中使用斗鱼订阅命令。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or conversation.chat_type != "group"
        or (context.channel_id is not None and int(conversation.channel_id or 0) != int(context.channel_id))
    ):
        return None, None, _error_result("当前群聊上下文无效。", "trusted_context_required")
    return context, conversation, None


def _load_active_binding(db: Session, context: PluginExecutionContext) -> BotPlugin | None:
    """重新加载任务绑定，防止停用绑定或机器人继续产生主动消息。"""

    binding = db.get(BotPlugin, int(context.binding_id))
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or binding.plugin is None
        or binding.plugin.code != "douyu"
    ):
        return None
    return binding


def _update_mapping_result(
    mapping: dict[str, list[str]],
    *,
    field_name: str,
    conversation_key: str,
    value: str,
    add: bool,
    message: str,
) -> PluginExecutionResult:
    """返回完整订阅映射补丁；删除到空集合时保留空数组覆盖旧值。"""

    updated = {key: list(values) for key, values in mapping.items()}
    current = set(updated.get(conversation_key, []))
    if add:
        current.add(value)
    else:
        current.discard(value)
    # 平台当前配置补丁按字典深合并。空数组必须显式保留，才能覆盖绑定中该会话
    # 的旧订阅；读取配置时空数组不会参与调度或展示。
    updated[conversation_key] = sorted(current)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=message,
        output={
            "provider_status": "configured",
            "config_patch": {field_name: updated},
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _list_mapping_result(
    mapping: dict[str, list[str]],
    conversation_key: str,
    *,
    empty_text: str,
    title: str,
) -> PluginExecutionResult:
    items = mapping.get(conversation_key, [])
    text = empty_text if not items else title + "\n" + "\n".join(items)
    return _text_result(text, provider_status="completed")


def _text_result(text: str, *, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text,
        output={"provider_status": provider_status, "replies": standard_replies([text_reply(text)])},
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status, "replies": standard_replies([text_reply(message)])},
    )


def _resolve_operation(payload: Mapping[str, Any]) -> str:
    explicit = str(payload.get("operation") or "").strip().lower()
    if explicit:
        return explicit
    command = str(payload.get("command_name") or "").strip()
    if not command:
        command = str(payload.get("content") or "").strip().split(" ", 1)[0]
    return {
        "斗鱼订阅": "subscribe_room",
        "取消斗鱼订阅": "unsubscribe_room",
        "斗鱼订阅列表": "list_rooms",
        "斗鱼订阅提醒": "subscribe_reminder",
        "取消斗鱼订阅提醒": "unsubscribe_reminder",
        "开启斗鱼高能推送": "enable_highlights",
        "关闭斗鱼高能推送": "disable_highlights",
        "斗鱼高能推送状态": "highlight_status",
        "订阅鱼吧": "subscribe_yuba",
        "取消订阅鱼吧": "unsubscribe_yuba",
        "鱼吧订阅列表": "list_yubas",
    }.get(command, "room_status")


def _resolve_room_id(payload: Mapping[str, Any]) -> str:
    return _resolve_argument(payload, "room_id")


def _resolve_yuba_id(payload: Mapping[str, Any]) -> str:
    return _resolve_argument(payload, "hash_id")


def _resolve_argument(payload: Mapping[str, Any], field_name: str) -> str:
    for key in (field_name, "command_args"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    parts = [part for part in str(payload.get("content") or "").strip().split() if part]
    return parts[1] if len(parts) >= 2 else ""


def _load_config(raw: Mapping[str, Any]) -> DouyuConfig:
    """只读取当前配置字段；旧运行状态和旧推送开关不会进入新业务路径。"""

    return DouyuConfig(
        api_url_template=str(raw.get("api_url_template") or DEFAULT_ROOM_API_URL_TEMPLATE).strip(),
        yuba_api_url=str(raw.get("yuba_api_url") or DEFAULT_YUBA_API_URL).strip(),
        highlight_api_url=str(raw.get("highlight_api_url") or DEFAULT_HIGHLIGHT_API_URL).strip(),
        timeout_seconds=max(3, min(_coerce_int(raw.get("timeout_seconds"), 10), 30)),
        feed_limit=max(1, min(_coerce_int(raw.get("feed_limit"), 3), 10)),
        user_agent=str(raw.get("user_agent") or DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
        admin_user_ids=tuple(sorted(set(_normalize_text_list(raw.get("admin_user_ids"), maximum=100)))),
        room_subscriptions=_normalize_subscription_mapping(
            raw.get("room_subscriptions"),
            value_pattern=ROOM_ID_PATTERN,
        ),
        yuba_subscriptions=_normalize_subscription_mapping(
            raw.get("yuba_subscriptions"),
            value_pattern=YUBA_ID_PATTERN,
        ),
        reminder_subscribers=_normalize_subscription_mapping(raw.get("reminder_subscribers")),
        highlight_push_conversation_ids=tuple(_normalize_conversation_ids(raw.get("highlight_push_conversation_ids"))),
        highlight_enabled=_coerce_bool(raw.get("highlight_enabled"), True),
        highlight_interval_seconds=max(60, min(_coerce_int(raw.get("highlight_interval_seconds"), 600), 3600)),
        highlight_retention_days=max(1, min(_coerce_int(raw.get("highlight_retention_days"), 14), 365)),
        highlight_realtime_max_items=max(1, min(_coerce_int(raw.get("highlight_realtime_max_items"), 5), 10)),
        poll_interval_seconds=max(30, min(_coerce_int(raw.get("poll_interval_seconds"), 300), 3600)),
    )


def _normalize_subscription_mapping(
    value: Any,
    *,
    value_pattern: re.Pattern[str] | None = None,
) -> dict[str, list[str]]:
    """订阅映射只接受内部会话主键，旧 external_id 键不会被隐式继续使用。"""

    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, list[str]] = {}
    for raw_key, raw_items in value.items():
        conversation_key = str(raw_key or "").strip()
        if _positive_int(conversation_key) is None or not isinstance(raw_items, list):
            continue
        items = {
            str(item or "").strip()
            for item in raw_items
            if str(item or "").strip()
            and (value_pattern is None or value_pattern.fullmatch(str(item or "").strip()))
        }
        normalized[conversation_key] = sorted(items)
    return normalized


def _normalize_conversation_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if _positive_int(item) is not None}, key=int)


def _normalize_text_list(value: Any, *, maximum: int) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= maximum:
            break
    return result


def _all_mapping_values(mapping: dict[str, list[str]]) -> list[str]:
    return sorted({item for items in mapping.values() for item in items if item})


def _targets_for_value(mapping: dict[str, list[str]], value: str) -> list[str]:
    return sorted([key for key, items in mapping.items() if value in items], key=int)


def _delivery_state_key(conversation_key: str, room_id: str) -> str:
    return f"{conversation_key}:{room_id}"


def _interval_elapsed(last_fetch_at: str, current: datetime, interval_seconds: int) -> bool:
    try:
        previous = ensure_shanghai_datetime(datetime.fromisoformat(last_fetch_at))
    except (TypeError, ValueError):
        return True
    return (ensure_shanghai_datetime(current) - previous).total_seconds() >= interval_seconds


def _clean_text(value: Any, maximum: int) -> str:
    normalized = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    if maximum > 0 and len(normalized) > maximum:
        return normalized[: maximum - 1].rstrip() + "…"
    return normalized


def _positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
