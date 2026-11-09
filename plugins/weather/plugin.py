from __future__ import annotations

import gzip
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel, Conversation, WeatherSubscription
from app.services.outbound_batch import enqueue_outbound_jobs
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import standard_replies, text_reply


COMMANDS = ("天气", "订阅天气", "取消订阅")
SCHEDULE_ACTION_KEY = "weather_daily_push"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; MultiBotSaaS-Weather/2.0)"


@dataclass(frozen=True)
class WeatherConfig:
    """保存租户天气服务配置，敏感密钥只在当前执行请求内存在。"""

    api_base: str
    api_key: str
    default_city: str
    timeout_seconds: int
    daily_push_time: str


def matches_event(context) -> dict[str, object]:
    """匹配最新天气命令，以及 A-BOT 保留的“城市天气”自然查询形式。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = _strip_message_prefix(str(getattr(context, "content", "") or ""))
    command = _resolve_command(content)
    matched = (
        chat_type in {"direct", "group"}
        and message_type == "text"
        and (command in COMMANDS or (content.endswith("天气") and content != "天气订阅"))
    )
    return {
        "matched": matched,
        "reason": "weather_business_hit" if matched else "weather_business_command_required",
        "debug": {
            "command_name": command,
            "chat_type": chat_type,
            "message_type": message_type,
        },
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """处理后台重载和无状态天气查询；订阅写操作必须进入可信消息入口。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="天气插件已就绪。",
            output={
                "api_base_configured": bool(config.api_base),
                "api_key_configured": bool(config.api_key),
                "default_city": config.default_city,
                "daily_push_time": config.daily_push_time,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    if _resolve_operation(request.payload) != "query":
        return _error_result("天气订阅只能通过真实聊天会话操作。", provider_status="trusted_context_required")
    return _execute_query(request, config)


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """使用平台租户会话处理查询、订阅和退订，不读取载荷中的身份字段。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    operation = _resolve_operation(request.payload)
    if operation == "subscribe":
        return _subscribe_weather(db, request, config)
    if operation == "unsubscribe":
        return _unsubscribe_weather(db, request)
    return _execute_query(request, config)


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """通过平台统一调度器批量推送当前租户、当前机器人下的天气订阅。"""

    if request.action != SCHEDULE_ACTION_KEY:
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的定时动作: {request.action}")
    config = _load_config(request.config)
    config_error = _validate_runtime_config(config)
    if config_error:
        return config_error
    context = request.context
    if context is None:
        return _error_result("天气定时任务缺少可信绑定上下文。", provider_status="trusted_context_required")

    subscriptions = db.scalars(
        select(WeatherSubscription)
        .where(
            WeatherSubscription.tenant_id == int(context.tenant_id),
            WeatherSubscription.bot_id == int(context.bot_id),
            WeatherSubscription.status == "active",
        )
        .order_by(WeatherSubscription.id.asc())
    ).all()
    if not subscriptions:
        return PluginExecutionResult(
            ok=True,
            status="ignored",
            message="当前机器人没有天气订阅。",
            output={"provider_status": "no_subscriptions", "subscription_count": 0},
        )

    # 同一城市只请求一次天气接口；同一会话、同一城市只创建一个出站任务，避免多人订阅
    # 时重复刷屏。订阅者身份仍分别保存在关系表中，退订不会影响同群其他成员。
    subscriptions_by_city: dict[str, list[WeatherSubscription]] = defaultdict(list)
    for subscription in subscriptions:
        subscriptions_by_city[str(subscription.city_id or "").strip()].append(subscription)

    current = now_shanghai()
    queued_conversations = 0
    successful_subscriptions = 0
    failed_subscriptions = 0
    requested_cities = 0
    for city_id, city_subscriptions in subscriptions_by_city.items():
        if not city_id:
            _mark_subscriptions_failed(city_subscriptions, "city_id_missing")
            failed_subscriptions += len(city_subscriptions)
            continue
        try:
            first_subscription = city_subscriptions[0]
            previous = _parse_weather_snapshot(first_subscription.latest_snapshot_json)
            weather = _fetch_weather_by_location(
                config,
                city_id=city_id,
                city_name=str(first_subscription.city_name or city_id),
                previous=previous,
            )
            requested_cities += 1
        except (PluginHttpError, ValueError, json.JSONDecodeError):
            _mark_subscriptions_failed(city_subscriptions, "weather_fetch_failed")
            failed_subscriptions += len(city_subscriptions)
            continue

        subscriptions_by_conversation: dict[int, list[WeatherSubscription]] = defaultdict(list)
        for subscription in city_subscriptions:
            conversation_id = _positive_int(subscription.conversation_id)
            if conversation_id is None:
                subscription.last_error = "conversation_missing"
                failed_subscriptions += 1
                continue
            subscriptions_by_conversation[conversation_id].append(subscription)

        for conversation_id, target_subscriptions in subscriptions_by_conversation.items():
            conversation = db.get(Conversation, conversation_id)
            channel = db.get(Channel, int(conversation.channel_id or 0)) if conversation else None
            if (
                conversation is None
                or int(conversation.tenant_id) != int(context.tenant_id)
                or int(conversation.bot_id) != int(context.bot_id)
                or channel is None
                or int(channel.tenant_id) != int(context.tenant_id)
                or not channel.is_enabled
            ):
                _mark_subscriptions_failed(target_subscriptions, "conversation_unavailable")
                failed_subscriptions += len(target_subscriptions)
                continue

            previous = _parse_weather_snapshot(target_subscriptions[0].latest_snapshot_json)
            reply_text = _build_daily_push_text(weather, previous=previous)
            enqueue_outbound_jobs(
                db,
                conversation=conversation,
                channel=channel,
                payloads=[_build_text_outbound_payload(conversation, reply_text)],
                plugin_code="weather",
                related_type="weather_subscription",
                related_id=int(target_subscriptions[0].id),
                priority=70,
            )
            queued_conversations += 1
            successful_subscriptions += len(target_subscriptions)
            for subscription in target_subscriptions:
                subscription.last_push_at = current
                subscription.next_push_at = _next_push_time(config.daily_push_time, after=current)
                subscription.last_error = None
                subscription.latest_snapshot_json = _serialize_weather_snapshot(weather)
                # 历史实现曾把完整租户配置连同 API Key 复制到订阅表。新主线只从
                # 当前绑定密文读取密钥，处理到的旧记录在此顺带清空明文副本。
                subscription.config_json = None

    db.flush()
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"天气早报已为 {queued_conversations} 个会话创建推送任务。",
        output={
            "provider_status": "completed",
            "subscription_count": len(subscriptions),
            "requested_city_count": requested_cities,
            "queued_conversation_count": queued_conversations,
            "successful_subscription_count": successful_subscriptions,
            "failed_subscription_count": failed_subscriptions,
        },
    )


def _execute_query(request: PluginExecutionRequest, config: WeatherConfig) -> PluginExecutionResult:
    config_error = _validate_runtime_config(config)
    if config_error:
        return config_error
    city = _pick_city(request.payload, config.default_city)
    if not city:
        return _error_result("请指定要查询的城市。", provider_status="city_required")
    try:
        weather = _fetch_weather_bundle(config, city=city)
    except (PluginHttpError, ValueError, json.JSONDecodeError):
        return _error_result("天气查询暂时不可用，请检查城市或租户天气服务配置。", provider_status="fetch_failed")

    reply_text = _build_weather_report_text(weather)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已返回 {weather['city_name']} 的天气。",
        output={
            "provider_status": "completed",
            "city": weather["city_name"],
            "city_id": weather["city_id"],
            "forecast_date": weather["forecast_date"],
            "condition_day": weather["condition_day"],
            "temp_min": weather["temp_min"],
            "temp_max": weather["temp_max"],
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def _subscribe_weather(
    db: Session,
    request: PluginExecutionRequest,
    config: WeatherConfig,
) -> PluginExecutionResult:
    config_error = _validate_runtime_config(config)
    if config_error:
        return config_error
    context = request.context
    if context is None or context.conversation_id is None or not str(context.sender_id or "").strip():
        return _error_result("订阅天气需要真实聊天会话和发送人身份。", provider_status="trusted_context_required")

    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
    ):
        return _error_result("当前会话不属于这个机器人。", provider_status="conversation_mismatch")

    city = _pick_city(request.payload, "")
    if not city:
        return _error_result("请指定订阅城市，例如：订阅天气 长沙。", provider_status="city_required")
    try:
        weather = _fetch_weather_bundle(config, city=city)
    except (PluginHttpError, ValueError, json.JSONDecodeError):
        return _error_result("没有找到这个城市，或天气服务暂时不可用。", provider_status="fetch_failed")

    sender_id = str(context.sender_id).strip()
    subscription = db.scalar(
        select(WeatherSubscription)
        .where(
            WeatherSubscription.tenant_id == int(context.tenant_id),
            WeatherSubscription.bot_id == int(context.bot_id),
            WeatherSubscription.conversation_id == int(conversation.id),
            WeatherSubscription.subscriber_id == sender_id,
        )
        .order_by(WeatherSubscription.id.desc())
        .limit(1)
    )
    if subscription is None:
        subscription = WeatherSubscription(
            tenant_id=int(context.tenant_id),
            bot_id=int(context.bot_id),
            channel_id=int(conversation.channel_id) if conversation.channel_id else None,
            conversation_id=int(conversation.id),
            chat_type=str(conversation.chat_type or "direct"),
            target_external_id=str(conversation.external_id or ""),
            subscriber_id=sender_id,
            city_query=city,
            city_name=weather["city_name"],
            city_id=weather["city_id"],
        )
        db.add(subscription)
    else:
        subscription.channel_id = int(conversation.channel_id) if conversation.channel_id else None
        subscription.chat_type = str(conversation.chat_type or "direct")
        subscription.target_external_id = str(conversation.external_id or "")
        subscription.city_query = city
        subscription.city_name = weather["city_name"]
        subscription.city_id = weather["city_id"]

    push_hour, _ = _parse_clock_time(config.daily_push_time)
    subscription.schedule_hour = push_hour
    subscription.status = "active"
    subscription.next_push_at = _next_push_time(config.daily_push_time)
    subscription.last_error = None
    subscription.latest_snapshot_json = _serialize_weather_snapshot(weather)
    subscription.config_json = None
    db.flush()

    reply_text = (
        "天气订阅已开启\n"
        f"城市：{weather['city_name']}\n"
        f"推送时间：每天 {config.daily_push_time}\n"
        f"下次推送：{_format_datetime(subscription.next_push_at)}"
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="天气订阅已开启。",
        output={
            "provider_status": "subscribed",
            "subscription_id": int(subscription.id),
            "city": weather["city_name"],
            "city_id": weather["city_id"],
            "next_push_at": subscription.next_push_at.isoformat() if subscription.next_push_at else None,
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def _unsubscribe_weather(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    context = request.context
    if context is None or context.conversation_id is None or not str(context.sender_id or "").strip():
        return _error_result("取消订阅需要真实聊天会话和发送人身份。", provider_status="trusted_context_required")

    subscriptions = db.scalars(
        select(WeatherSubscription).where(
            WeatherSubscription.tenant_id == int(context.tenant_id),
            WeatherSubscription.bot_id == int(context.bot_id),
            WeatherSubscription.conversation_id == int(context.conversation_id),
            WeatherSubscription.subscriber_id == str(context.sender_id).strip(),
            WeatherSubscription.status == "active",
        )
    ).all()
    if not subscriptions:
        return _error_result("当前会话没有你的天气订阅。", provider_status="subscription_not_found")
    for subscription in subscriptions:
        subscription.status = "cancelled"
        subscription.next_push_at = None
        subscription.last_error = None
        subscription.config_json = None
    db.flush()
    reply_text = "天气订阅已取消。"
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=reply_text,
        output={
            "provider_status": "unsubscribed",
            "cancelled_count": len(subscriptions),
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def _fetch_weather_bundle(config: WeatherConfig, *, city: str) -> dict[str, Any]:
    """先解析标准城市，再用城市 ID 获取三日天气，避免重复执行 Geo 查询。"""

    client = PluginHttpClient()
    city_meta = _lookup_city(client, config, city)
    city_id = str(city_meta.get("id") or "").strip()
    if not city_id:
        raise ValueError("weather city id missing")
    forecast = _fetch_forecast(client, config, city_id)
    return _build_weather_snapshot(city_meta, forecast)


def _fetch_weather_by_location(
    config: WeatherConfig,
    *,
    city_id: str,
    city_name: str,
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    """定时推送直接复用订阅保存的城市 ID，不重复消耗城市检索接口。"""

    forecast = _fetch_forecast(PluginHttpClient(), config, city_id)
    city_meta = {
        "id": city_id,
        "name": city_name,
        "adm1": str(previous.get("adm1") or ""),
        "country": str(previous.get("country") or ""),
    }
    return _build_weather_snapshot(city_meta, forecast)


def _lookup_city(client: PluginHttpClient, config: WeatherConfig, city: str) -> Mapping[str, Any]:
    url = _build_api_url(
        config.api_base,
        "/geo/v2/city/lookup",
        {"location": city, "key": config.api_key, "lang": "zh"},
    )
    payload = _request_weather_json(client, url, timeout_seconds=config.timeout_seconds)
    locations = payload.get("location")
    if not isinstance(locations, list) or not locations or not isinstance(locations[0], Mapping):
        raise ValueError("weather city not found")
    return locations[0]


def _fetch_forecast(client: PluginHttpClient, config: WeatherConfig, city_id: str) -> Mapping[str, Any]:
    url = _build_api_url(
        config.api_base,
        "/v7/weather/3d",
        {"location": city_id, "key": config.api_key, "lang": "zh"},
    )
    payload = _request_weather_json(client, url, timeout_seconds=config.timeout_seconds)
    daily = payload.get("daily")
    if not isinstance(daily, list) or not daily or not isinstance(daily[0], Mapping):
        raise ValueError("weather forecast missing")
    return payload


def _request_weather_json(
    client: PluginHttpClient,
    url: str,
    *,
    timeout_seconds: int,
) -> Mapping[str, Any]:
    """通过平台受控 HTTP 客户端读取 JSON，并限制响应体、重定向和重试次数。"""

    response = client.request(
        "GET",
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        connect_timeout_seconds=min(8, timeout_seconds),
        total_timeout_seconds=timeout_seconds,
        max_response_bytes=1024 * 1024,
        max_redirects=2,
        retry_count=1,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"weather http {response.status_code}")
    body = gzip.decompress(response.body) if response.body[:2] == b"\x1f\x8b" else response.body
    payload = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(payload, Mapping):
        raise ValueError("weather response must be object")
    if str(payload.get("code") or "").strip() != "200":
        raise ValueError("weather service rejected request")
    return payload


def _build_weather_snapshot(
    city_meta: Mapping[str, Any],
    forecast: Mapping[str, Any],
) -> dict[str, Any]:
    daily = forecast.get("daily")
    today = daily[0] if isinstance(daily, list) and daily and isinstance(daily[0], Mapping) else {}
    return {
        "city_id": str(city_meta.get("id") or "").strip(),
        "city_name": str(city_meta.get("name") or "").strip(),
        "adm1": str(city_meta.get("adm1") or "").strip(),
        "country": str(city_meta.get("country") or "").strip(),
        "forecast_date": str(today.get("fxDate") or "").strip(),
        "condition_day": str(today.get("textDay") or "未知").strip() or "未知",
        "condition_night": str(today.get("textNight") or "未知").strip() or "未知",
        "temp_min": _optional_number(today.get("tempMin")),
        "temp_max": _optional_number(today.get("tempMax")),
        "humidity": _optional_number(today.get("humidity")),
        "precipitation": _optional_number(today.get("precip")),
        "uv_index": _optional_number(today.get("uvIndex")),
        "sunrise": str(today.get("sunrise") or "").strip(),
        "sunset": str(today.get("sunset") or "").strip(),
        "wind_direction": str(today.get("windDirDay") or "").strip(),
        "wind_scale": str(today.get("windScaleDay") or "").strip(),
    }


def _build_weather_report_text(weather: Mapping[str, Any]) -> str:
    lines = [
        f"{weather['city_name']}天气：{weather['condition_day']}",
        f"温度：{_temperature_range(weather)}",
    ]
    if weather.get("humidity") is not None:
        lines.append(f"湿度：{weather['humidity']}%")
    if weather.get("wind_scale"):
        lines.append(f"风力：{weather.get('wind_direction') or '风'} {weather['wind_scale']}级")
    if weather.get("sunrise") or weather.get("sunset"):
        lines.append(f"日出/日落：{weather.get('sunrise') or '-'} / {weather.get('sunset') or '-'}")
    return "\n".join(lines)


def _build_daily_push_text(
    weather: Mapping[str, Any],
    *,
    previous: Mapping[str, Any],
) -> str:
    location = " ".join(
        item for item in (str(weather.get("adm1") or ""), str(weather.get("city_name") or "")) if item
    )
    lines = [
        f"天气早报 {location}".strip(),
        f"日期：{weather.get('forecast_date') or '-'}",
        f"白天：{weather.get('condition_day') or '未知'}，{_temperature_range(weather)}",
        f"夜间：{weather.get('condition_night') or '未知'}",
    ]
    alerts = _build_weather_alerts(weather, previous=previous)
    if alerts:
        lines.extend(["", "特别提醒", *[f"- {item}" for item in alerts]])
    details: list[str] = []
    if weather.get("humidity") is not None:
        details.append(f"湿度 {weather['humidity']}%")
    if weather.get("precipitation") is not None:
        details.append(f"降水 {weather['precipitation']} mm")
    if weather.get("wind_scale"):
        details.append(f"{weather.get('wind_direction') or '风'} {weather['wind_scale']}级")
    if details:
        lines.extend(["", "生活信息：" + "，".join(details)])
    lines.append("穿衣建议：" + _build_clothing_advice(weather))
    lines.append("回复“取消订阅”可停止天气早报。")
    return "\n".join(lines)


def _build_weather_alerts(
    weather: Mapping[str, Any],
    *,
    previous: Mapping[str, Any],
) -> list[str]:
    """从当日预报和上次快照提取少量高价值提醒，不生成无依据的天气结论。"""

    alerts: list[str] = []
    temp_min = _optional_number(weather.get("temp_min"))
    temp_max = _optional_number(weather.get("temp_max"))
    if temp_min is not None and temp_max is not None and temp_max - temp_min >= 12:
        alerts.append(f"昼夜温差 {temp_max - temp_min:g}°C，早晚注意增减衣物。")
    previous_max = _optional_number(previous.get("temp_max"))
    if temp_max is not None and previous_max is not None:
        change = temp_max - previous_max
        if change <= -6:
            alerts.append(f"最高温较上次预报下降 {abs(change):g}°C。")
        elif change >= 6:
            alerts.append(f"最高温较上次预报上升 {change:g}°C。")
    condition = f"{weather.get('condition_day') or ''}{weather.get('condition_night') or ''}"
    if any(keyword in condition for keyword in ("暴雨", "大雨", "雷", "冰雹")):
        alerts.append("存在强降水或雷电天气，外出注意安全。")
    elif "雪" in condition:
        alerts.append("可能出现降雪，注意道路湿滑。")
    wind_scale = _max_wind_scale(weather.get("wind_scale"))
    if wind_scale >= 7:
        alerts.append("风力较强，远离临时搭建物和高空坠物风险区域。")
    uv_index = _optional_number(weather.get("uv_index"))
    if uv_index is not None and uv_index >= 8:
        alerts.append("紫外线较强，长时间户外活动注意防晒。")
    return alerts


def _build_clothing_advice(weather: Mapping[str, Any]) -> str:
    temp_min = _optional_number(weather.get("temp_min"))
    temp_max = _optional_number(weather.get("temp_max"))
    temperatures = [item for item in (temp_min, temp_max) if item is not None]
    if not temperatures:
        return "根据体感灵活增减衣物。"
    average = sum(temperatures) / len(temperatures)
    if average <= 5:
        return "建议穿厚外套并注意保暖。"
    if average <= 15:
        return "建议穿外套或针织衫。"
    if average <= 25:
        return "建议穿长袖或薄外套。"
    return "建议穿轻薄透气衣物并注意补水。"


def _build_text_outbound_payload(conversation: Conversation, content: str) -> dict[str, Any]:
    return {
        "external_id": str(conversation.external_id or ""),
        "receiver_id": str(conversation.external_id or ""),
        "chat_type": str(conversation.chat_type or "direct"),
        "message_type": "text",
        "content": content,
    }


def _validate_runtime_config(config: WeatherConfig) -> PluginExecutionResult | None:
    if not config.api_base or not config.api_key:
        return _error_result("请先配置当前租户的天气 API 地址和密钥。", provider_status="weather_not_configured")
    return None


def _error_result(message: str, *, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _load_config(raw_config: Mapping[str, Any]) -> WeatherConfig:
    raw = dict(raw_config)
    return WeatherConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        default_city=str(raw.get("default_city") or "北京").strip() or "北京",
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=15, minimum=5, maximum=60),
        daily_push_time=_normalize_clock_time(str(raw.get("daily_push_time") or "08:00")),
    )


def _resolve_operation(payload: Mapping[str, Any]) -> str:
    content = _strip_message_prefix(str(payload.get("content") or payload.get("message_text") or ""))
    command_name = str(payload.get("command_name") or "").strip()
    if command_name == "订阅天气" or content == "订阅天气" or content.startswith("订阅天气 "):
        return "subscribe"
    if command_name == "取消订阅" or content == "取消订阅" or content.startswith("取消订阅 "):
        return "unsubscribe"
    return "query"


def _pick_city(payload: Mapping[str, Any], default_city: str) -> str:
    explicit = str(payload.get("city") or "").strip()
    if explicit:
        return explicit
    command_args = str(payload.get("command_args") or "").strip()
    if command_args:
        return command_args
    content = _strip_message_prefix(
        str(payload.get("content") or payload.get("message_text") or payload.get("query") or "")
    )
    if content.startswith("订阅天气"):
        return content[len("订阅天气") :].strip()
    if content == "天气":
        return default_city
    if content.startswith("天气 "):
        return content[len("天气") :].strip() or default_city
    if content.endswith("天气") and content != "天气":
        return content[: -len("天气")].strip()
    return default_city


def _resolve_command(content: str) -> str:
    if content == "天气" or content.startswith("天气 "):
        return "天气"
    for command in ("订阅天气", "取消订阅"):
        if content == command or content.startswith(f"{command} "):
            return command
    return ""


def _strip_message_prefix(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    return re.sub(r"\s+", " ", normalized)


def _build_api_url(api_base: str, path: str, parameters: Mapping[str, Any]) -> str:
    parsed = urlsplit(api_base)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise PluginHttpError("天气 API 地址不合法")
    base = api_base.rstrip("/")
    return f"{base}{path}?{urlencode({key: str(value) for key, value in parameters.items()})}"


def _serialize_weather_snapshot(weather: Mapping[str, Any]) -> str:
    """订阅表只保存公开天气字段，不保存 API 地址、密钥或上游完整响应。"""

    allowed_fields = {
        "city_id",
        "city_name",
        "adm1",
        "country",
        "forecast_date",
        "condition_day",
        "condition_night",
        "temp_min",
        "temp_max",
        "humidity",
        "precipitation",
        "uv_index",
        "sunrise",
        "sunset",
        "wind_direction",
        "wind_scale",
    }
    return json.dumps(
        {key: weather.get(key) for key in allowed_fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _parse_weather_snapshot(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _mark_subscriptions_failed(
    subscriptions: list[WeatherSubscription],
    error_code: str,
) -> None:
    for subscription in subscriptions:
        subscription.last_error = error_code
        subscription.config_json = None


def _next_push_time(clock_time: str, *, after: datetime | None = None) -> datetime:
    base = ensure_shanghai_datetime(after or now_shanghai())
    hour, minute = _parse_clock_time(clock_time)
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= base:
        candidate += timedelta(days=1)
    return candidate


def _normalize_clock_time(value: str) -> str:
    try:
        hour, minute = _parse_clock_time(value)
    except ValueError:
        hour, minute = 8, 0
    return f"{hour:02d}:{minute:02d}"


def _parse_clock_time(value: str) -> tuple[int, int]:
    normalized = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", normalized)
    if not match:
        raise ValueError("clock time invalid")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError("clock time invalid")
    return hour, minute


def _temperature_range(weather: Mapping[str, Any]) -> str:
    minimum = weather.get("temp_min")
    maximum = weather.get("temp_max")
    return f"{minimum if minimum is not None else '-'}~{maximum if maximum is not None else '-'}°C"


def _format_datetime(value: datetime | None) -> str:
    return ensure_shanghai_datetime(value).strftime("%Y-%m-%d %H:%M") if value else "未安排"


def _max_wind_scale(value: Any) -> int:
    numbers = [int(item) for item in re.findall(r"\d+", str(value or ""))]
    return max(numbers) if numbers else 0


def _optional_number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _positive_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default
