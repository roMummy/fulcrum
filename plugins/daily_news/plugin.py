from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from typing import Any
from urllib import request as urlrequest

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Conversation, Plugin
from app.services.outbound_batch import enqueue_outbound_jobs_by_conversation
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


DEFAULT_USER_AGENT = "multi-bot-admin/1.0"
CHINESE_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


@dataclass(frozen=True)
class DailyNewsConfig:
    baidu_api_url: str
    important_news_api_url: str
    news60s_api_url: str
    default_timeout: int
    baidu_limit: int
    push_time: str
    push_all_channels: bool
    last_push_date: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="daily_news runtime is ready",
            output={"push_time": config.push_time, "push_all_channels": config.push_all_channels},
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"unsupported action: {request.action}")

    operation = _resolve_operation(request.payload)
    try:
        if operation == "baidu":
            text = _get_baidu_news(config)
            return _result("fetched", text, image_base64=None)
        if operation == "important":
            text = _get_important_news(config)
            return _result("fetched", text, image_base64=None)
        if operation == "60s":
            image_base64 = _get_60s_image_base64(config)
            text = _build_digest_title("60秒新闻速览")
            return _result("fetched", text, image_base64=image_base64)
        payload = _build_daily_digest(config)
        return _result("fetched", payload["text"], image_base64=payload.get("image_base64"))
    except Exception as exc:
        return PluginExecutionResult(ok=False, status="error", message=f"daily_news failed: {exc}")


def run_daily_news_push(binding_id: int) -> dict[str, Any]:
    now = datetime.now().astimezone()
    with SessionLocal() as db:
        binding = db.get(BotPlugin, int(binding_id or 0))
        if binding is None or binding.status != "enabled" or binding.bot is None or not binding.bot.is_enabled:
            return {"skipped": True}
        plugin = binding.plugin or db.scalar(select(Plugin).where(Plugin.id == binding.plugin_id).limit(1))
        if plugin is None or plugin.code != "daily_news":
            return {"skipped": True}
        config = _load_config(_parse_json(binding.config_json))
        if not _should_push_now(now, config.push_time, config.last_push_date):
            return {"skipped": True, "reason": "not_due"}

        digest = _build_daily_digest(config)
        conversations = _resolve_target_conversations(db, binding.bot_id, config.push_all_channels)
        batch_result = enqueue_outbound_jobs_by_conversation(
            db,
            conversations=conversations,
            plugin_code="daily_news",
            related_type="daily_news_binding",
            related_id=binding.id,
            priority=70,
            payload_builder=lambda conversation, channel: _build_push_payloads(conversation, digest),
            log_context={"binding_id": binding.id},
        )
        job_ids = list(batch_result["job_ids"])
        failed_targets = list(batch_result["failed_targets"])
        pushed_conversation_count = int(batch_result["pushed_conversation_count"] or 0)

        updated = _load_config(_parse_json(binding.config_json))
        binding.config_json = json.dumps(_dump_config(updated, last_push_date=now.strftime("%Y-%m-%d")), ensure_ascii=False, sort_keys=True)
        binding.last_execution_at = now_shanghai()
        binding.last_execution_status = "completed"
        summary = f"daily_news pushed to {pushed_conversation_count} conversations"
        if failed_targets:
            summary += f", skipped {len(failed_targets)} failed conversations"
        binding.last_execution_summary = summary
        db.commit()
        return {
            "ok": True,
            "conversation_count": pushed_conversation_count,
            "outbound_job_ids": job_ids,
            "failed_targets": failed_targets,
        }


def _result(provider_status: str, text: str, *, image_base64: str | None) -> PluginExecutionResult:
    replies: list[dict[str, str]] = []
    if image_base64:
        replies.append({"type": "image", "base64": image_base64})
    if text.strip():
        replies.append({"type": "text", "content": text.strip()})
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="daily news generated",
        output={"provider_status": provider_status, "reply_text": text.strip(), "image_base64": image_base64 or "", "replies": replies},
    )


def _build_daily_digest(config: DailyNewsConfig) -> dict[str, str]:
    headline = _build_digest_title("每日新闻")
    important = _get_important_news(config)
    baidu = _get_baidu_news(config)
    image_base64 = _get_60s_image_base64(config)
    parts = [headline]
    if important:
        parts.append(important)
    if baidu:
        parts.append(baidu)
    return {"text": "\n\n".join(part for part in parts if part.strip()), "image_base64": image_base64 or ""}


def _get_important_news(config: DailyNewsConfig) -> str:
    body = _post_form(
        config.important_news_api_url,
        {"type": "telegram", "keyword": "你需要知道的隔夜全球要闻", "page": 0, "rn": 1, "os": "web", "sv": "7.7.5", "app": "CailianpressWeb"},
        timeout=config.default_timeout,
    )
    item = ((((body.get("data") or {}).get("telegram") or {}).get("data") or [None])[0]) or {}
    descr = str(item.get("descr") or "").strip()
    timestamp = int(item.get("time") or 0)
    if not descr or not timestamp:
        return ""
    dt = datetime.fromtimestamp(timestamp)
    clean = re.sub(r"<[^>]+>", "", descr)
    clean = unescape(clean).replace("\xa0", " ")
    clean = re.sub(r"(\d{1,2}[、\.])", r"\n\1", clean)
    clean = re.sub(r"\n{2,}", "\n", clean).strip()
    return f"隔夜要闻 {dt.strftime('%Y-%m-%d')} {CHINESE_WEEKDAYS[dt.weekday()]}\n{clean}"


def _get_baidu_news(config: DailyNewsConfig) -> str:
    body = _get_json(config.baidu_api_url, timeout=config.default_timeout)
    cards = ((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get("cards") or []
    lines = ["百度热搜"]
    index = 1
    for card in cards:
        contents = card.get("content") if isinstance(card, dict) else []
        for block in contents or []:
            articles = block.get("content") if isinstance(block, dict) else []
            for article in articles or []:
                if not isinstance(article, dict):
                    continue
                word = str(article.get("word") or "").strip()
                if not word:
                    continue
                lines.append(f"{index}. {word}")
                index += 1
                if index > config.baidu_limit:
                    return "\n".join(lines)
    return "\n".join(lines[: max(1, config.baidu_limit + 1)])


def _get_60s_image_base64(config: DailyNewsConfig) -> str | None:
    body = _get_json(config.news60s_api_url, timeout=config.default_timeout)
    image_url = str(((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get("image") or "").strip()
    if not image_url:
        return None
    raw = _download_binary(image_url, timeout=config.default_timeout)
    return base64.b64encode(raw).decode("ascii") if raw else None


def _resolve_operation(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("operation") or payload.get("mode") or "").strip().lower()
    if explicit:
        return explicit
    first = str(payload.get("command_name") or payload.get("matched_keyword") or "").strip()
    if not first:
        first = str(payload.get("content") or "").strip().split(" ")[0]
    mapping = {
        "今日新闻": "daily",
        "百度新闻": "baidu",
        "隔夜要闻": "important",
        "60s新闻": "60s",
    }
    return mapping.get(first, "daily")


def _build_digest_title(title: str) -> str:
    now = datetime.now()
    return f"{title}\n{now.strftime('%Y-%m-%d')} {CHINESE_WEEKDAYS[now.weekday()]}"


def _should_push_now(now: datetime, push_time: str, last_push_date: str) -> bool:
    today = now.strftime("%Y-%m-%d")
    if last_push_date == today:
        return False
    hour, minute = _parse_push_time(push_time)
    return (now.hour, now.minute) >= (hour, minute)


def _parse_push_time(value: str) -> tuple[int, int]:
    text = str(value or "08:30").strip()
    if ":" not in text:
        return 8, 30
    hour_text, minute_text = text.split(":", 1)
    try:
        return max(0, min(23, int(hour_text))), max(0, min(59, int(minute_text)))
    except ValueError:
        return 8, 30


def _resolve_target_conversations(db, bot_id: int, push_all_channels: bool) -> list[Conversation]:
    group_items = db.scalars(
        select(Conversation)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(Conversation.bot_id == bot_id, Conversation.chat_type == "group", Channel.is_enabled.is_(True))
        .order_by(Conversation.id.asc())
    ).all()
    if group_items:
        return group_items
    if not push_all_channels:
        return []
    return db.scalars(
        select(Conversation)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(Conversation.bot_id == bot_id, Channel.is_enabled.is_(True))
        .order_by(Conversation.id.asc())
    ).all()


def _build_push_payloads(conversation: Conversation, digest: dict[str, str]) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    image_base64 = str(digest.get("image_base64") or "").strip()
    text = str(digest.get("text") or "").strip()
    if image_base64:
        payloads.append({"external_id": conversation.external_id, "receiver_id": conversation.external_id, "chat_type": conversation.chat_type, "message_type": "image", "base64": image_base64})
    if text:
        payloads.append({"external_id": conversation.external_id, "receiver_id": conversation.external_id, "chat_type": conversation.chat_type, "message_type": "text", "content": text})
    return payloads


def _get_json(url: str, *, timeout: int) -> dict[str, Any]:
    req = urlrequest.Request(url, method="GET", headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")


def _post_form(url: str, data: dict[str, Any], *, timeout: int) -> dict[str, Any]:
    encoded = "&".join(f"{key}={_quote_plus(value)}" for key, value in data.items()).encode("utf-8")
    req = urlrequest.Request(url, data=encoded, method="POST", headers={"User-Agent": DEFAULT_USER_AGENT, "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        parsed = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("provider returned non-object json")


def _download_binary(url: str, *, timeout: int) -> bytes:
    req = urlrequest.Request(url, method="GET", headers={"User-Agent": DEFAULT_USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as response:
        return response.read()


def _quote_plus(value: Any) -> str:
    from urllib.parse import quote_plus

    return quote_plus(str(value))


def _load_config(raw: dict[str, Any] | Any) -> DailyNewsConfig:
    data = dict(raw) if isinstance(raw, dict) else {}
    return DailyNewsConfig(
        baidu_api_url=str(data.get("baidu_api_url") or "https://top.baidu.com/api/board?platform=wise&tab=realtime").strip(),
        important_news_api_url=str(data.get("important_news_api_url") or "https://www.cls.cn/api/sw?app=CailianpressWeb&os=web&sv=7.7.5").strip(),
        news60s_api_url=str(data.get("news60s_api_url") or "http://192.168.2.32:4399/v2/60s").strip(),
        default_timeout=max(5, _coerce_int(data.get("default_timeout"), 15)),
        baidu_limit=max(5, _coerce_int(data.get("baidu_limit"), 15)),
        push_time=str(data.get("push_time") or "08:30").strip() or "08:30",
        push_all_channels=bool(data.get("push_all_channels", True)),
        last_push_date=str(data.get("last_push_date") or "").strip(),
    )


def _dump_config(config: DailyNewsConfig, *, last_push_date: str) -> dict[str, Any]:
    return {
        "baidu_api_url": config.baidu_api_url,
        "important_news_api_url": config.important_news_api_url,
        "news60s_api_url": config.news60s_api_url,
        "default_timeout": config.default_timeout,
        "baidu_limit": config.baidu_limit,
        "push_time": config.push_time,
        "push_all_channels": config.push_all_channels,
        "last_push_date": last_push_date,
    }


def _parse_json(raw: str | None) -> dict[str, Any]:
    normalized = str(raw or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
