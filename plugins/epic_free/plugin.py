from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    Conversation,
    GroupPluginScope,
    TaskJob,
)
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore


PLUGIN_CODE = "epic_free"
SCHEDULE_ACTION_KEY = "poll_free_games"
STATE_NAMESPACE = "epic_free_delivery"
EPIC_API_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
MANAGED_GROUP_CHANNEL_TYPES = {"wechat_ipad", "wechat_winauto", "workwechat"}
LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}-[A-Za-z]{2}$")
COUNTRY_PATTERN = re.compile(r"^[A-Za-z]{2}$")
logger = get_logger(__name__)


@dataclass(frozen=True)
class EpicFreeConfig:
    """保存当前插件绑定唯一生效的查询和推送配置。"""

    locale: str
    country: str
    include_upcoming: bool
    only_on_change: bool
    poll_interval_seconds: int
    timeout_seconds: int


@dataclass(frozen=True)
class EpicGame:
    """保存从 Epic 官方响应中提取的最小免费活动信息。"""

    offer_id: str
    title: str
    seller: str
    state: str
    start_at: datetime | None
    end_at: datetime | None
    original_price: str
    discount_price: str
    link: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """提供后台健康检查和人工查询，不接受载荷覆盖地区或查询地址。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="Epic 免费游戏插件运行正常。",
            output={
                "locale": config.locale,
                "country": config.country,
                "poll_interval_seconds": config.poll_interval_seconds,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    try:
        games = _fetch_free_games(config)
    except (PluginHttpError, ValueError, json.JSONDecodeError):
        return _error_result("Epic 免费游戏查询暂时不可用，请稍后重试。", "fetch_failed")

    reply_text = _render_games_text(games, config)
    current_count, upcoming_count = _count_games(games)
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已查询到 {current_count} 个当前免费游戏。",
        output={
            "provider_status": "completed",
            "current_count": current_count,
            "upcoming_count": upcoming_count,
            "replies": standard_replies([text_reply(reply_text)]),
        },
    )


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """按统一调度动作查询官网，并只向尚未收到当前摘要的群聊入队。"""

    if request.action != SCHEDULE_ACTION_KEY:
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的 Epic 免费游戏动作: {request.action}",
        )
    binding, context_error = _require_scheduled_binding(db, request)
    if context_error is not None or binding is None:
        return context_error or _error_result("Epic 免费游戏缺少可信运行上下文。", "trusted_context_required")

    config = _load_config(request.config)
    try:
        games = _fetch_free_games(config)
    except (PluginHttpError, ValueError, json.JSONDecodeError):
        return _error_result("Epic 免费游戏查询暂时不可用，请稍后重试。", "fetch_failed")

    conversations = _resolve_target_conversations(db, binding)
    if not conversations:
        return _error_result("当前机器人没有可推送的启用群聊。", "target_empty")

    digest = _build_games_digest(games, config)
    current_count, upcoming_count = _count_games(games)
    reply_text = _render_games_text(games, config)
    store = PluginStateStore(db, request.context)
    current = now_shanghai()
    queued_job_ids: list[int] = []
    skipped_conversation_ids: list[int] = []
    failed_conversation_ids: list[int] = []

    for conversation in conversations:
        state_key = _delivery_state_key(conversation, config)
        delivery_state = store.get(STATE_NAMESPACE, state_key)
        if config.only_on_change and str((delivery_state or {}).get("digest") or "") == digest:
            skipped_conversation_ids.append(int(conversation.id))
            continue

        job_id = _enqueue_conversation_push(
            db,
            binding=binding,
            conversation=conversation,
            content=reply_text,
            digest=digest,
            config=config,
            current=current,
        )
        if job_id is None:
            failed_conversation_ids.append(int(conversation.id))
            continue

        queued_job_ids.append(job_id)
        # 摘要按会话保存。部分群入队失败时，成功群不会在下一轮重复收到同一活动，
        # 失败群仍会继续重试；新启用的群也能独立收到当前内容。
        store.put(
            STATE_NAMESPACE,
            state_key,
            {
                "digest": digest,
                "current_count": current_count,
                "upcoming_count": upcoming_count,
                "queued_at": current.isoformat(),
            },
        )

    db.flush()
    if failed_conversation_ids:
        return PluginExecutionResult(
            ok=False,
            status="partial",
            message=f"Epic 免费游戏已向 {len(queued_job_ids)} 个群聊入队，{len(failed_conversation_ids)} 个群聊失败。",
            output={
                "provider_status": "partial",
                "queued_conversation_count": len(queued_job_ids),
                "skipped_conversation_count": len(skipped_conversation_ids),
                "failed_conversation_ids": failed_conversation_ids,
                "outbound_job_ids": queued_job_ids,
            },
        )
    if not queued_job_ids:
        return PluginExecutionResult(
            ok=True,
            status="skipped",
            message="Epic 免费游戏内容没有变化，已跳过推送。",
            output={
                "provider_status": "unchanged",
                "target_count": len(conversations),
                "skipped_conversation_count": len(skipped_conversation_ids),
                "current_count": current_count,
                "upcoming_count": upcoming_count,
            },
        )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"Epic 免费游戏已向 {len(queued_job_ids)} 个群聊创建推送任务。",
        output={
            "provider_status": "completed",
            "target_count": len(conversations),
            "queued_conversation_count": len(queued_job_ids),
            "skipped_conversation_count": len(skipped_conversation_ids),
            "current_count": current_count,
            "upcoming_count": upcoming_count,
            "outbound_job_ids": queued_job_ids,
        },
    )


def _require_scheduled_binding(
    db: Session,
    request: PluginExecutionRequest,
) -> tuple[BotPlugin | None, PluginExecutionResult | None]:
    """交叉验证调度器重建的租户、绑定和机器人身份。"""

    context = request.context
    if context is None:
        return None, _error_result("Epic 免费游戏缺少可信运行上下文。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or plugin_code != PLUGIN_CODE
    ):
        return None, _error_result("Epic 免费游戏插件绑定不存在或未启用。", "binding_unavailable")
    return binding, None


def _resolve_target_conversations(
    db: Session,
    binding: BotPlugin,
) -> list[Conversation]:
    """返回当前机器人允许推送的群聊，并遵守绑定范围和群级停用设置。"""

    conversations = db.scalars(
        select(Conversation)
        .join(Channel, Channel.id == Conversation.channel_id)
        .where(
            Conversation.bot_id == int(binding.bot_id),
            Conversation.chat_type == "group",
            Channel.is_enabled.is_(True),
        )
        .order_by(Conversation.id.asc())
    ).all()
    # allow_from/deny_from 属于平台绑定范围，而非 Epic 业务配置。这里直接从绑定
    # 原始普通配置读取，确保定时推送与消息分发使用同一会话准入语义。
    try:
        parsed_binding_config = json.loads(str(binding.config_json or "").strip() or "{}")
    except json.JSONDecodeError:
        parsed_binding_config = {}
    binding_config = parsed_binding_config if isinstance(parsed_binding_config, dict) else {}
    allow_from = _normalize_scope_values(binding_config.get("allow_from"))
    deny_from = _normalize_scope_values(binding_config.get("deny_from"))

    resolved: list[Conversation] = []
    for conversation in conversations:
        external_id = str(conversation.external_id or "").strip()
        if not external_id or external_id in deny_from or (allow_from and external_id not in allow_from):
            continue
        channel = db.get(Channel, int(conversation.channel_id or 0))
        if channel is None or int(channel.bot_id) != int(binding.bot_id) or not channel.is_enabled:
            continue
        if str(channel.channel_type or "").strip() in MANAGED_GROUP_CHANNEL_TYPES:
            chatroom = db.scalar(
                select(Chatroom).where(
                    Chatroom.channel_id == int(channel.id),
                    Chatroom.external_id == external_id,
                )
            )
            if chatroom is not None and not bool(chatroom.is_active):
                continue
            if chatroom is not None:
                scope = db.scalar(
                    select(GroupPluginScope).where(
                        GroupPluginScope.channel_id == int(channel.id),
                        GroupPluginScope.chatroom_id == int(chatroom.id),
                        GroupPluginScope.bot_plugin_id == int(binding.id),
                    )
                )
                if scope is not None and not bool(scope.enabled):
                    continue
        resolved.append(conversation)
    return resolved


def _enqueue_conversation_push(
    db: Session,
    *,
    binding: BotPlugin,
    conversation: Conversation,
    content: str,
    digest: str,
    config: EpicFreeConfig,
    current: datetime,
) -> int | None:
    """通过通用出站队列幂等创建一条群推送任务。"""

    channel = db.get(Channel, int(conversation.channel_id or 0))
    if channel is None or not channel.is_enabled:
        return None
    dedupe_key = _delivery_dedupe_key(binding, conversation, digest, config, current)
    existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
    if existing is not None:
        return int(existing.id)
    try:
        # SAVEPOINT 只回滚本会话的并发重复入队，不破坏同一轮已经为其他群创建的任务。
        with db.begin_nested():
            job = enqueue_outbound_job(
                db,
                conversation=conversation,
                channel=channel,
                payload={
                    "external_id": conversation.external_id,
                    "receiver_id": conversation.external_id,
                    "chat_type": "group",
                    "message_type": "text",
                    "content": content,
                },
                plugin_code=PLUGIN_CODE,
                related_type="epic_free_binding",
                related_id=int(binding.id),
                priority=70,
                dedupe_key=dedupe_key,
                max_attempts=3,
            )
        return int(job.id)
    except IntegrityError:
        existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
        return int(existing.id) if existing is not None else None
    except Exception as exc:
        logger.warning(
            "epic free outbound enqueue failed",
            extra={
                "binding_id": int(binding.id),
                "conversation_id": int(conversation.id),
                "error_type": type(exc).__name__,
            },
        )
        return None


def _fetch_free_games(config: EpicFreeConfig) -> list[EpicGame]:
    """使用平台受控 HTTP 客户端读取并清洗 Epic 官方免费活动。"""

    query = urlencode(
        {
            "locale": config.locale,
            "country": config.country,
            "allowCountries": config.country,
        }
    )
    response = PluginHttpClient().request(
        "GET",
        f"{EPIC_API_URL}?{query}",
        headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
        connect_timeout_seconds=config.timeout_seconds,
        total_timeout_seconds=config.timeout_seconds,
        max_response_bytes=4 * 1024 * 1024,
        retry_count=1,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise ValueError(f"Epic 接口返回 HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Epic 接口返回的 JSON 不是对象")
    data = _as_dict(payload.get("data"))
    catalog = _as_dict(data.get("Catalog"))
    search_store = _as_dict(catalog.get("searchStore"))
    elements = _as_list(search_store.get("elements"))
    current = _now_utc()
    games: list[EpicGame] = []
    for raw_element in elements:
        element = _as_dict(raw_element)
        title = str(element.get("title") or "").strip()
        if not title:
            continue
        promotions = _as_dict(element.get("promotions"))
        groups = (
            ("current", _as_list(promotions.get("promotionalOffers"))),
            ("upcoming", _as_list(promotions.get("upcomingPromotionalOffers"))),
        )
        for state, buckets in groups:
            if state == "upcoming" and not config.include_upcoming:
                continue
            for raw_bucket in buckets:
                bucket = _as_dict(raw_bucket)
                for raw_promotion in _as_list(bucket.get("promotionalOffers")):
                    promotion = _as_dict(raw_promotion)
                    discount = _as_dict(promotion.get("discountSetting"))
                    if str(discount.get("discountType") or "").upper() != "PERCENTAGE":
                        continue
                    if _safe_int(discount.get("discountPercentage"), default=-1) != 0:
                        continue
                    start_at = _parse_utc_datetime(promotion.get("startDate"))
                    end_at = _parse_utc_datetime(promotion.get("endDate"))
                    if state == "current" and ((start_at and current < start_at) or (end_at and current >= end_at)):
                        continue
                    if state == "upcoming" and start_at is not None and start_at <= current:
                        continue
                    games.append(_build_game(element, title, state, start_at, end_at, config.locale))

    # 同一 offer 可能出现在多个促销 bucket。只按稳定业务字段去重，不保存第三方完整响应。
    unique: dict[tuple[str, str, str, str], EpicGame] = {}
    for game in games:
        key = (
            game.offer_id or game.title,
            game.state,
            _format_digest_time(game.start_at),
            _format_digest_time(game.end_at),
        )
        unique[key] = game
    result = list(unique.values())
    result.sort(
        key=lambda item: (
            0 if item.state == "current" else 1,
            item.end_at or datetime.max.replace(tzinfo=timezone.utc),
            item.start_at or datetime.max.replace(tzinfo=timezone.utc),
            item.title,
        )
    )
    return result


def _build_game(
    element: Mapping[str, Any],
    title: str,
    state: str,
    start_at: datetime | None,
    end_at: datetime | None,
    locale: str,
) -> EpicGame:
    """从单个官网条目提取展示所需字段。"""

    total_price = _as_dict(_as_dict(element.get("price")).get("totalPrice"))
    price_text = _as_dict(total_price.get("fmtPrice"))
    seller = str(_as_dict(element.get("seller")).get("name") or "").strip()
    return EpicGame(
        offer_id=str(element.get("id") or "").strip(),
        title=title,
        seller=seller,
        state=state,
        start_at=start_at,
        end_at=end_at,
        original_price=str(price_text.get("originalPrice") or "-").strip() or "-",
        discount_price=str(price_text.get("discountPrice") or "0").strip() or "0",
        link=_safe_offer_link(locale, element),
    )


def _safe_offer_link(locale: str, element: Mapping[str, Any]) -> str:
    """只在 Epic 官方域名下拼接经过 URL 编码的产品路径。"""

    mappings = _as_list(_as_dict(element.get("catalogNs")).get("mappings"))
    page_slug = str(_as_dict(mappings[0]).get("pageSlug") or "").strip() if mappings else ""
    product_slug = str(element.get("productSlug") or "").strip().replace("/home", "")
    slug = (page_slug or product_slug).strip("/")
    if slug:
        return f"https://store.epicgames.com/{locale}/p/{quote(slug, safe='/-_.~')}"
    return f"https://store.epicgames.com/{locale}/free-games"


def _render_games_text(games: list[EpicGame], config: EpicFreeConfig) -> str:
    """生成渠道无关的简洁中文业务文本。"""

    current_games = [item for item in games if item.state == "current"]
    upcoming_games = [item for item in games if item.state == "upcoming"]
    lines = [
        "Epic 官方免费游戏",
        f"地区：{config.country}  语言：{config.locale}",
        f"官方页面：https://store.epicgames.com/{config.locale}/free-games",
        "",
        f"当前可领取（{len(current_games)}）：" if current_games else "当前可领取：暂无",
    ]
    for index, game in enumerate(current_games, start=1):
        lines.extend(
            [
                f"{index}. {game.title}",
                f"发行商：{game.seller or '-'}",
                f"价格：{game.original_price} -> {game.discount_price}",
                f"截止：{_format_display_time(game.end_at)}",
                f"链接：{game.link}",
            ]
        )
    if config.include_upcoming:
        lines.extend(["", f"即将免费（{len(upcoming_games)}）：" if upcoming_games else "即将免费：暂无"])
        for index, game in enumerate(upcoming_games, start=1):
            lines.extend(
                [
                    f"{index}. {game.title}",
                    f"开始：{_format_display_time(game.start_at)}",
                    f"结束：{_format_display_time(game.end_at)}",
                    f"链接：{game.link}",
                ]
            )
    return "\n".join(lines)


def _build_games_digest(games: list[EpicGame], config: EpicFreeConfig) -> str:
    """用稳定字段生成摘要，避免标题描述调整之外的第三方噪声触发推送。"""

    normalized = [
        {
            "offer_id": game.offer_id,
            "title": game.title,
            "state": game.state,
            "start_at": _format_digest_time(game.start_at),
            "end_at": _format_digest_time(game.end_at),
            "original_price": game.original_price,
            "discount_price": game.discount_price,
            "link": game.link,
        }
        for game in games
    ]
    payload = json.dumps(
        {
            "locale": config.locale,
            "country": config.country,
            "include_upcoming": config.include_upcoming,
            "games": normalized,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _delivery_state_key(conversation: Conversation, config: EpicFreeConfig) -> str:
    """用会话和本地化配置摘要定位独立投递状态。"""

    config_key = hashlib.sha256(
        f"{config.locale}:{config.country}:{int(config.include_upcoming)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"conversation:{int(conversation.id)}:{config_key}"


def _delivery_dedupe_key(
    binding: BotPlugin,
    conversation: Conversation,
    digest: str,
    config: EpicFreeConfig,
    current: datetime,
) -> str:
    """内容去重模式按摘要幂等，固定推送模式按轮询时间桶幂等。"""

    if config.only_on_change:
        execution_key = digest[:32]
    else:
        interval = max(300, int(config.poll_interval_seconds))
        execution_key = str(int(current.timestamp()) // interval)
    return f"epic-free:{int(binding.id)}:{int(conversation.id)}:{execution_key}"


def _load_config(raw_config: Mapping[str, Any]) -> EpicFreeConfig:
    """读取唯一配置字段，并拒绝把任意文本拼入官方接口查询参数。"""

    raw = dict(raw_config)
    locale = str(raw.get("locale") or "zh-CN").strip()
    country = str(raw.get("country") or "CN").strip().upper()
    if not LOCALE_PATTERN.fullmatch(locale):
        locale = "zh-CN"
    else:
        language, region = locale.split("-", 1)
        locale = f"{language.lower()}-{region.upper()}"
    if not COUNTRY_PATTERN.fullmatch(country):
        country = "CN"
    return EpicFreeConfig(
        locale=locale,
        country=country,
        include_upcoming=_as_bool(raw.get("include_upcoming"), default=True),
        only_on_change=_as_bool(raw.get("only_on_change"), default=True),
        poll_interval_seconds=_bounded_int(raw.get("poll_interval_seconds"), default=3600, minimum=300, maximum=86400),
        timeout_seconds=_bounded_int(raw.get("timeout_seconds"), default=20, minimum=5, maximum=60),
    )


def _count_games(games: list[EpicGame]) -> tuple[int, int]:
    return sum(item.state == "current" for item in games), sum(item.state == "upcoming" for item in games)


def _parse_utc_datetime(value: Any) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_display_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return ensure_shanghai_datetime(value).strftime("%Y-%m-%d %H:%M")


def _format_digest_time(value: datetime | None) -> str:
    return value.astimezone(timezone.utc).isoformat() if value is not None else ""


def _now_utc() -> datetime:
    """统一取得可替换的 UTC 当前时间，便于活动窗口做稳定验证。"""

    return now_shanghai().astimezone(timezone.utc)


def _normalize_scope_values(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip() for item in value.replace("，", ",").split(",") if item.strip()}
    return set()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(_safe_int(value, default=default), maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """返回不暴露第三方响应和内部异常的业务错误。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status},
    )
