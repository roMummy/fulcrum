from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib import parse as urlparse
from urllib import request as urlrequest

from sqlalchemy import desc, select

from app.core.db import SessionLocal
from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation, TaskJob, WeatherSubscription
from app.services.outbound_batch import enqueue_outbound_jobs
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.task_queue import WEATHER_PUSH_JOB, enqueue_job


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_USER_AGENT = "multi-bot-admin/1.0"
DEFAULT_COMMANDS = ("天气", "weather", "订阅天气", "取消订阅", "天气订阅")
@dataclass(frozen=True)
class WeatherConfig:
    api_base: str
    api_key: str
    default_city: str
    units: str
    default_timeout: int
    default_push_hour: int


def _text_reply(content: str) -> list[dict[str, str]]:
    normalized = str(content).strip()
    return [{"type": "text", "content": normalized}] if normalized else []


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request.config)
    if request.action == "reload":
        if not config.api_base:
            return PluginExecutionResult(ok=False, status="error", message="天气插件缺少 api_base 配置。")
        return PluginExecutionResult(
            ok=True,
            status="running",
            message=f"天气插件已就绪，默认城市 {config.default_city}。",
            output={
                "api_base": config.api_base,
                "default_city": config.default_city,
                "units": config.units,
                "api_key_configured": bool(config.api_key),
                "default_timeout": config.default_timeout,
                "default_push_hour": config.default_push_hour,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")
    if not config.api_base:
        return PluginExecutionResult(ok=False, status="error", message="天气插件缺少 api_base 配置。")
    if not config.api_key:
        return PluginExecutionResult(ok=False, status="error", message="天气插件缺少 api_key 配置。")

    operation = _resolve_operation(request.payload)
    if operation == "subscribe":
        return _subscribe_weather(request, config)
    if operation == "unsubscribe":
        return _unsubscribe_weather(request)
    if operation == "list":
        return _list_subscriptions(request)

    city = _pick_city(request.payload, config.default_city)
    if not city:
        return PluginExecutionResult(ok=False, status="error", message="未识别到天气查询城市。")
    try:
        weather = _fetch_weather_bundle(api_base=config.api_base, api_key=config.api_key, city=city, units=config.units, timeout=config.default_timeout)
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"天气查询失败: {exc}")
    reply_text = _build_weather_report_text(weather)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"天气查询完成，已返回 {weather['city_name']} 的天气。",
        output={
            "provider_status": "fetched",
            "provider": weather["provider"],
            "city": weather["city_name"],
            "city_id": weather["city_id"],
            "requested_city": city,
            "units": config.units,
            "reply_text": reply_text,
            "weather": weather,
            "replies": _text_reply(reply_text),
        },
    )


def _subscribe_weather(request: PluginExecutionRequest, config: WeatherConfig) -> PluginExecutionResult:
    conversation_id = _coerce_int(request.payload.get("conversation_id"))
    sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
    channel_id = _coerce_int(request.payload.get("channel_id"))
    target_external_id = str(
        request.payload.get("conversation_external_id")
        or request.payload.get("external_id")
        or request.payload.get("room_id")
        or ""
    ).strip()
    chat_type = str(request.payload.get("chat_type") or "group").strip() or "group"
    city = _pick_city(request.payload, config.default_city)
    if not conversation_id or not target_external_id:
        return PluginExecutionResult(ok=False, status="error", message="订阅天气需要有效的 conversation_id 和会话 external_id。")
    if not city:
        return PluginExecutionResult(ok=False, status="error", message="订阅天气需要指定城市，例如：订阅天气 长沙。")

    try:
        weather = _fetch_weather_bundle(api_base=config.api_base, api_key=config.api_key, city=city, units=config.units, timeout=config.default_timeout)
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"订阅天气失败，城市校验未通过: {exc}")

    with SessionLocal() as db:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            return PluginExecutionResult(ok=False, status="error", message="未找到目标会话，无法创建天气订阅。")
        binding = db.scalar(
            select(WeatherSubscription)
            .where(
                WeatherSubscription.conversation_id == conversation.id,
                WeatherSubscription.subscriber_id == (sender_id or None),
                WeatherSubscription.status == "active",
            )
            .limit(1)
        )
        if binding is None:
            binding = WeatherSubscription(
                bot_id=conversation.bot_id,
                channel_id=conversation.channel_id,
                conversation_id=conversation.id,
                chat_type=conversation.chat_type,
                target_external_id=conversation.external_id,
                subscriber_id=sender_id or None,
                city_query=city,
                city_name=weather["city_name"],
                city_id=weather["city_id"],
                schedule_hour=config.default_push_hour,
                status="active",
                config_json=_serialize_config(config),
            )
            db.add(binding)
            db.flush()
        else:
            binding.bot_id = conversation.bot_id
            binding.channel_id = conversation.channel_id
            binding.chat_type = conversation.chat_type
            binding.target_external_id = conversation.external_id
            binding.city_query = city
            binding.city_name = weather["city_name"]
            binding.city_id = weather["city_id"]
            binding.schedule_hour = config.default_push_hour
            binding.status = "active"
            binding.last_error = None
            binding.config_json = _serialize_config(config)

        next_push_at = _next_push_time(config.default_push_hour)
        binding.next_push_at = next_push_at
        binding.latest_snapshot_json = json.dumps(weather, ensure_ascii=False)
        _cancel_pending_subscription_jobs(db, binding.id)
        job = _enqueue_weather_push_job(db, binding.id, next_push_at)
        db.commit()

    reply_text = (
        f"天气订阅已开启\n"
        f"城市: {weather['city_name']}\n"
        f"推送时间: 每天 {config.default_push_hour:02d}:00\n"
        f"下次推送: {next_push_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"订阅任务: #{job.id}"
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="天气订阅创建成功。",
        output={
            "provider_status": "subscribed",
            "subscription_id": binding.id,
            "job_id": job.id,
            "city": weather["city_name"],
            "city_id": weather["city_id"],
            "next_push_at": next_push_at.isoformat(),
            "reply_text": reply_text,
            "replies": _text_reply(reply_text),
        },
    )


def _unsubscribe_weather(request: PluginExecutionRequest) -> PluginExecutionResult:
    conversation_id = _coerce_int(request.payload.get("conversation_id"))
    sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
    if not conversation_id:
        return PluginExecutionResult(ok=False, status="error", message="取消订阅需要 conversation_id。")
    with SessionLocal() as db:
        stmt = select(WeatherSubscription).where(
            WeatherSubscription.conversation_id == conversation_id,
            WeatherSubscription.status == "active",
        )
        if sender_id:
            stmt = stmt.where(WeatherSubscription.subscriber_id == sender_id)
        items = db.scalars(stmt).all()
        if not items:
            return PluginExecutionResult(ok=False, status="error", message="当前没有可取消的天气订阅。")
        ids = [item.id for item in items]
        for item in items:
            item.status = "cancelled"
            item.next_push_at = None
        _cancel_pending_subscription_jobs(db, *ids)
        db.commit()
    reply_text = f"已取消 {len(ids)} 条天气订阅。"
    return PluginExecutionResult(ok=True, status="completed", message="天气订阅已取消。", output={"subscription_ids": ids, "reply_text": reply_text, "replies": _text_reply(reply_text)})


def _list_subscriptions(request: PluginExecutionRequest) -> PluginExecutionResult:
    conversation_id = _coerce_int(request.payload.get("conversation_id"))
    sender_id = str(request.payload.get("sender_id") or request.payload.get("user") or "").strip()
    with SessionLocal() as db:
        stmt = select(WeatherSubscription).where(WeatherSubscription.status == "active").order_by(desc(WeatherSubscription.id))
        if conversation_id:
            stmt = stmt.where(WeatherSubscription.conversation_id == conversation_id)
        if sender_id:
            stmt = stmt.where(WeatherSubscription.subscriber_id == sender_id)
        items = db.scalars(stmt.limit(20)).all()
    if not items:
        reply_text = "当前没有天气订阅。"
    else:
        lines = ["当前天气订阅："]
        for item in items:
            next_push = item.next_push_at.astimezone().strftime("%Y-%m-%d %H:%M:%S") if item.next_push_at else "未安排"
            lines.append(f"#{item.id} {item.city_name} 每天 {int(item.schedule_hour or 8):02d}:00 下次 {next_push}")
        reply_text = "\n".join(lines)
    return PluginExecutionResult(ok=True, status="completed", message="天气订阅列表生成完成。", output={"provider_status": "listed", "reply_text": reply_text, "replies": _text_reply(reply_text)})


def process_weather_push_job(payload: dict[str, Any]) -> dict[str, Any]:
    subscription_id = _coerce_int(payload.get("subscription_id"))
    if not subscription_id:
        raise ValueError("subscription_id_missing")
    with SessionLocal() as db:
        subscription = db.get(WeatherSubscription, subscription_id)
        if subscription is None:
            raise ValueError("weather_subscription_not_found")
        if subscription.status != "active":
            return {"skipped": True, "reason": "subscription_inactive"}
        conversation = db.get(Conversation, int(subscription.conversation_id or 0))
        if conversation is None:
            subscription.status = "error"
            subscription.last_error = "conversation_not_found"
            db.commit()
            raise ValueError("conversation_not_found")
        channel_id = int(subscription.channel_id or conversation.channel_id or 0)
        if not channel_id:
            subscription.status = "error"
            subscription.last_error = "channel_not_found"
            db.commit()
            raise ValueError("channel_not_found")
        channel = db.get(Channel, channel_id)
        if channel is None:
            subscription.status = "error"
            subscription.last_error = "channel_not_found"
            db.commit()
            raise ValueError("channel_not_found")
        if not channel.is_enabled:
            subscription.status = "error"
            subscription.last_error = "channel_disabled"
            db.commit()
            raise ValueError("channel_disabled")

        config = _load_config(_parse_json(subscription.config_json))
        weather = _fetch_weather_bundle(
            api_base=config.api_base,
            api_key=config.api_key,
            city=subscription.city_id or subscription.city_query,
            units=config.units,
            timeout=config.default_timeout,
        )
        reply_text = _build_daily_push_text(weather)
        outbound_payload = {
            "external_id": conversation.external_id,
            "receiver_id": conversation.external_id,
            "chat_type": conversation.chat_type,
            "message_type": "text",
            "content": reply_text,
        }
        job_ids = enqueue_outbound_jobs(
            db,
            conversation=conversation,
            channel=channel,
            payloads=[outbound_payload],
            plugin_code="weather",
            related_type="weather_subscription",
            related_id=subscription.id,
            priority=70,
        )
        next_push_at = _next_push_time(int(subscription.schedule_hour or 8), after=now_shanghai() + timedelta(minutes=1))
        subscription.last_push_at = now_shanghai()
        subscription.last_error = None
        subscription.next_push_at = next_push_at
        subscription.latest_snapshot_json = json.dumps(weather, ensure_ascii=False)
        _enqueue_weather_push_job(db, subscription.id, next_push_at)
        db.commit()
        return {
            "subscription_id": subscription.id,
            "city": weather["city_name"],
            "outbound_job_id": job_ids[0] if job_ids else None,
            "next_push_at": next_push_at.isoformat(),
        }


def _enqueue_weather_push_job(db, subscription_id: int, available_at: datetime) -> TaskJob:
    return enqueue_job(
        db,
        job_type=WEATHER_PUSH_JOB,
        payload={"subscription_id": subscription_id},
        priority=60,
        related_type="weather_subscription",
        related_id=subscription_id,
        available_at=available_at,
    )


def _cancel_pending_subscription_jobs(db, *subscription_ids: int) -> None:
    normalized = [item for item in subscription_ids if int(item or 0) > 0]
    if not normalized:
        return
    jobs = db.scalars(
        select(TaskJob).where(
            TaskJob.job_type == WEATHER_PUSH_JOB,
            TaskJob.related_type == "weather_subscription",
            TaskJob.related_id.in_(normalized),
            TaskJob.status.in_(("pending", "running")),
        )
    ).all()
    for job in jobs:
        job.status = "cancelled"
        job.finished_at = now_shanghai()


def _resolve_operation(payload: dict[str, Any] | Any) -> str:
    if not isinstance(payload, dict):
        return "query"
    explicit = str(payload.get("operation") or payload.get("mode") or "").strip().lower()
    if explicit in {"query", "subscribe", "unsubscribe", "list"}:
        return explicit
    content = str(payload.get("content") or "").strip()
    stripped = str(payload.get("stripped_content") or payload.get("command_args") or "").strip()
    text = content or stripped
    if text.startswith("订阅天气"):
        return "subscribe"
    if text.startswith("取消订阅"):
        return "unsubscribe"
    if text.startswith("天气订阅"):
        return "list"
    command_name = str(payload.get("command_name") or "").strip().lower()
    if command_name in {"订阅天气"}:
        return "subscribe"
    if command_name in {"取消订阅"}:
        return "unsubscribe"
    if command_name in {"天气订阅"}:
        return "list"
    return "query"


def _pick_city(payload: dict[str, Any] | Any, default_city: str) -> str:
    if not isinstance(payload, dict):
        return default_city
    explicit_city = str(payload.get("city") or "").strip()
    if explicit_city:
        return explicit_city
    for key in ("command_args", "stripped_query", "stripped_content", "stripped_message_text", "query", "content", "message_text"):
        extracted = _extract_city_from_command(str(payload.get(key) or "").strip(), default_city)
        if extracted:
            return extracted
    return default_city


def _extract_city_from_command(raw_text: str, default_city: str) -> str:
    normalized = raw_text.strip()
    if not normalized:
        return ""
    normalized = normalized.replace("\n", " ")
    for prefix in ("订阅天气", "取消订阅", "天气订阅", "/订阅天气", "/取消订阅", "/天气", "/weather", "天气", "weather"):
        if normalized.startswith(prefix):
            remainder = normalized[len(prefix):].strip()
            return remainder or ("" if "取消订阅" in prefix or "天气订阅" in prefix else default_city)
    return normalized


def _fetch_weather_bundle(*, api_base: str, api_key: str, city: str, units: str, timeout: int) -> dict[str, Any]:
    location = _resolve_qweather_location(api_base=api_base, api_key=api_key, city=city, timeout=timeout)
    provider = "qweather"
    request_url = f"{api_base.rstrip('/')}/v7/weather/3d?location={urlparse.quote(location)}&key={urlparse.quote(api_key)}&lang=zh"
    data = _get_json(request_url, timeout)
    daily = data.get("daily")
    if not isinstance(daily, list) or not daily:
        raise ValueError("provider returned empty daily forecast")
    today = daily[0] if isinstance(daily[0], dict) else {}
    city_meta = _resolve_qweather_city_meta(api_base=api_base, api_key=api_key, city=city, timeout=timeout)
    return {
        "provider": provider,
        "city_id": str(city_meta.get("id") or location).strip() or location,
        "city_name": str(city_meta.get("name") or city).strip() or city,
        "adm1": str(city_meta.get("adm1") or "").strip(),
        "country": str(city_meta.get("country") or "").strip(),
        "condition_day": str(today.get("textDay") or "未知").strip() or "未知",
        "condition_night": str(today.get("textNight") or "未知").strip() or "未知",
        "temp_min": _coerce_optional_number(today.get("tempMin")),
        "temp_max": _coerce_optional_number(today.get("tempMax")),
        "humidity": _coerce_optional_number(today.get("humidity")),
        "precip": _coerce_optional_number(today.get("precip")),
        "uv_index": _coerce_optional_number(today.get("uvIndex")),
        "sunrise": str(today.get("sunrise") or "").strip(),
        "sunset": str(today.get("sunset") or "").strip(),
        "wind_dir": str(today.get("windDirDay") or "").strip(),
        "wind_scale": str(today.get("windScaleDay") or "").strip(),
        "fx_date": str(today.get("fxDate") or "").strip(),
        "source_url": str(data.get("fxLink") or "").strip(),
    }


def _build_weather_report_text(weather: dict[str, Any]) -> str:
    parts = [f"{weather['city_name']}天气：{weather['condition_day']}"]
    if weather.get("temp_min") is not None or weather.get("temp_max") is not None:
        parts.append(f"{weather.get('temp_min', '-') }~{weather.get('temp_max', '-') }°C".replace("None", "-"))
    if weather.get("humidity") is not None:
        parts.append(f"湿度 {weather['humidity']}%")
    if weather.get("wind_scale"):
        parts.append(f"{weather.get('wind_dir') or '风'} {weather['wind_scale']}级")
    if weather.get("sunrise") or weather.get("sunset"):
        parts.append(f"日出{weather.get('sunrise') or '-'} 日落{weather.get('sunset') or '-'}")
    return "，".join(parts)


def _build_daily_push_text(weather: dict[str, Any]) -> str:
    city_prefix = "".join(part for part in (weather.get("country"), weather.get("adm1")) if part)
    title = f"{city_prefix} {weather['city_name']}".strip()
    lines = [f"天气早报 {title}", f"日期: {weather.get('fx_date') or '-'}", ""]
    lines.append(f"白天: {weather['condition_day']}  {weather.get('temp_min', '-') }~{weather.get('temp_max', '-') }°C".replace("None", "-"))
    lines.append(f"夜间: {weather['condition_night']}")
    if weather.get("humidity") is not None:
        lines.append(f"湿度: {weather['humidity']}%")
    if weather.get("precip") is not None:
        lines.append(f"降水: {weather['precip']} mm")
    if weather.get("wind_scale"):
        lines.append(f"风力: {weather.get('wind_dir') or '风'} {weather['wind_scale']}级")
    if weather.get("uv_index") is not None:
        lines.append(f"紫外线: {weather['uv_index']}")
    if weather.get("sunrise") or weather.get("sunset"):
        lines.append(f"日出/日落: {weather.get('sunrise') or '-'} / {weather.get('sunset') or '-'}")
    return "\n".join(lines)


def _resolve_qweather_city_meta(*, api_base: str, api_key: str, city: str, timeout: int) -> dict[str, Any]:
    normalized_city = city.strip()
    if not normalized_city:
        return {}
    base_root = api_base.rstrip("/")
    if "/v7/" in base_root:
        base_root = base_root.split("/v7/", 1)[0]
    lookup_url = (
        f"{base_root}/geo/v2/city/lookup?location={urlparse.quote(normalized_city)}"
        f"&key={urlparse.quote(api_key)}&lang=zh"
    )
    data = _get_json(lookup_url, timeout)
    locations = data.get("location")
    if not isinstance(locations, list) or not locations:
        raise ValueError(f"未找到城市: {city}")
    first = locations[0] if isinstance(locations[0], dict) else {}
    return first


def _resolve_qweather_location(*, api_base: str, api_key: str, city: str, timeout: int) -> str:
    normalized_city = city.strip()
    if not normalized_city or normalized_city.isdigit():
        return normalized_city
    first = _resolve_qweather_city_meta(api_base=api_base, api_key=api_key, city=city, timeout=timeout)
    return str(first.get("id") or normalized_city).strip() or normalized_city


def _get_json(url: str, timeout: int) -> dict[str, Any]:
    req = urlrequest.Request(url, method="GET", headers={"User-Agent": DEFAULT_USER_AGENT, "Accept-Encoding": "gzip"})
    with urlrequest.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT_SECONDS) as response:
        body = response.read()
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    raw = body.decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    if isinstance(parsed, dict):
        code = str(parsed.get("code") or "").strip()
        if code and code != "200":
            raise ValueError(f"provider returned code={code}")
        return parsed
    raise ValueError("provider returned non-object json")


def _load_config(raw: dict[str, Any] | Any) -> WeatherConfig:
    data = dict(raw) if isinstance(raw, dict) else {}
    return WeatherConfig(
        api_base=str(data.get("api_base") or "https://mx564thnfv.re.qweatherapi.com").strip(),
        api_key=str(data.get("api_key") or "").strip(),
        default_city=str(data.get("default_city") or "Beijing").strip() or "Beijing",
        units=str(data.get("units") or "metric").strip() or "metric",
        default_timeout=max(5, _coerce_int(data.get("default_timeout")) or 10),
        default_push_hour=min(23, max(0, _coerce_int(data.get("default_push_hour")) or 8)),
    )


def _serialize_config(config: WeatherConfig) -> str:
    return json.dumps(
        {
            "api_base": config.api_base,
            "api_key": config.api_key,
            "default_city": config.default_city,
            "units": config.units,
            "default_timeout": config.default_timeout,
            "default_push_hour": config.default_push_hour,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _parse_json(raw: str | None) -> dict[str, Any]:
    normalized = str(raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _next_push_time(hour: int, *, after: datetime | None = None) -> datetime:
    base = after or now_shanghai()
    candidate = base.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= base:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(now_shanghai().tzinfo)


def _coerce_optional_number(value: Any) -> int | float | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        number = float(normalized)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
