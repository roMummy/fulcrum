from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.markdown_to_image import convert_md_str_to_image_sync
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    Message,
    TaskJob,
)
from app.services.outbound_batch import enqueue_outbound_job
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.http_client import PluginHttpClient, PluginHttpError
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore


PLUGIN_CODE = "dota2_stats"
OPEN_DOTA_API_BASE = "https://api.opendota.com/api"
REGISTER_COMMAND = "注册Dota"
QUERY_COMMAND = "Dota战绩"
GROUP_RANKING_COMMAND = "Dota群排行"
GROUP_PREPARE_ACTION = "dota2_group_ranking_prepare"
GROUP_PUSH_ACTION = "dota2_group_ranking_push"
USER_BINDING_NAMESPACE = "dota2_user_bindings"
PERSONAL_CACHE_NAMESPACE = "dota2_personal_reports"
HERO_NAMES_NAMESPACE = "dota2_hero_names"
GROUP_ACCOUNT_NAMESPACE = "dota2_group_accounts"
GROUP_SNAPSHOT_NAMESPACE = "dota2_group_snapshots"
STEAM_ID64_BASE = 76561197960265728
MAX_ACCOUNT_ID = 2**32 - 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTA_IMAGE_ROOT = PROJECT_ROOT / "data" / "temp" / "md2image"
RANK_NAMES = {
    1: "先锋",
    2: "卫士",
    3: "中军",
    4: "统帅",
    5: "传奇",
    6: "万古流芳",
    7: "超凡入圣",
    8: "冠绝一世",
}
GAME_MODE_NAMES = {
    1: "全英雄选择",
    2: "队长模式",
    3: "随机征召",
    4: "单一征召",
    5: "全随机",
    12: "生疏模式",
    16: "队长征召",
    22: "全英雄选择",
    23: "加速模式",
}


class Dota2StatsError(ValueError):
    """Dota 账号、公开数据或 OpenDota 响应不满足当前业务要求。"""


@dataclass(frozen=True)
class Dota2Config:
    """保存当前租户绑定唯一生效的查询、限流和群榜配置。"""

    api_key: str
    request_timeout_seconds: int
    retry_count: int
    user_requests_per_minute: int
    dynamic_cache_seconds: int
    summary_match_limit: int
    recent_match_limit: int
    render_as_image: bool
    group_ranking_window_days: int
    group_ranking_limit: int
    minimum_win_rate_matches: int
    max_group_players: int
    snapshot_retention_days: int


@dataclass(frozen=True)
class TrustedDotaMessage:
    """保存经过数据库交叉验证的插件绑定、渠道、会话和入站消息。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message
    sender_id: str
    sender_name: str


def matches_event(context) -> dict[str, object]:
    """只匹配三个完整 Dota 业务命令，普通聊天中的游戏名称不会触发。"""

    config = _load_config(getattr(context, "binding_config", {}) or {})
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "text") or "text").strip().lower()
    action, _argument = _parse_command(str(getattr(context, "content", "") or ""))
    matched = chat_type in {"direct", "group"} and message_type == "text" and bool(action)
    return {
        "matched": matched,
        "reason": "dota2_command_hit" if matched else "dota2_command_required",
        "debug": {"action": action, "chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通运行入口只返回健康状态，真实查询必须来自可信消息或定时任务。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="Dota 2 战绩插件运行正常。",
            output={
                "api_key_configured": bool(config.api_key),
                "summary_match_limit": config.summary_match_limit,
                "recent_match_limit": config.recent_match_limit,
                "group_ranking_window_days": config.group_ranking_window_days,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在聊天中使用 Dota 2 战绩命令。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """执行账号绑定、个人查询或当天群榜读取，所有身份均从可信消息重建。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    trusted, context_error = _require_trusted_message(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _error_result("当前消息上下文无效。", "trusted_context_required")

    action, argument = _parse_command(str(trusted.message.content or ""))
    if not action:
        return _error_result("请使用注册Dota、Dota战绩或Dota群排行。", "command_invalid")
    decision = PluginRateLimiter(db, trusted.execution).consume(
        "user",
        identity=trusted.sender_id,
        limit=config.user_requests_per_minute,
        window_seconds=60,
        now=now_shanghai(),
    )
    if not decision.allowed:
        return _error_result(
            f"Dota 2 查询过于频繁，请 {decision.retry_after_seconds} 秒后再试。",
            "rate_limited",
        )

    store = PluginStateStore(db, trusted.execution)
    try:
        if action == "register":
            return _handle_register(store, config=config, trusted=trusted, raw_id=argument)
        if action == "query":
            return _handle_query(store, config=config, trusted=trusted, raw_id=argument)
        return _handle_group_ranking(store, config=config, trusted=trusted)
    except Dota2StatsError as exc:
        return _error_result(str(exc), "business_error")
    except (PluginHttpError, ValueError):
        return _error_result("OpenDota 服务暂时不可用，请稍后重试。", "provider_unavailable")


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """准备群榜时联网采集；推送阶段严格只读取当前租户当天快照。"""

    if request.action not in {GROUP_PREPARE_ACTION, GROUP_PUSH_ACTION}:
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的 Dota 2 群排行动作: {request.action}",
        )
    config = _load_config(request.config)
    binding, conversation, channel, error = _require_scheduled_group(db, request.context)
    if error is not None or binding is None or conversation is None or channel is None:
        return error or _error_result("Dota 2 群排行缺少可信运行上下文。", "trusted_context_required")
    context = request.context
    if context is None:
        return _error_result("Dota 2 群排行缺少可信运行上下文。", "trusted_context_required")
    store = PluginStateStore(db, context)
    stat_date = now_shanghai().date().isoformat()

    if request.action == GROUP_PREPARE_ACTION:
        try:
            return _prepare_group_snapshot(
                db,
                store=store,
                config=config,
                conversation=conversation,
                stat_date=stat_date,
            )
        except (PluginHttpError, ValueError):
            return _error_result("Dota 2 群排行采集失败，请稍后补跑。", "provider_unavailable")
    return _push_group_snapshot(
        db,
        store=store,
        config=config,
        binding=binding,
        conversation=conversation,
        channel=channel,
        stat_date=stat_date,
    )


def _handle_register(
    store: PluginStateStore,
    *,
    config: Dota2Config,
    trusted: TrustedDotaMessage,
    raw_id: str,
) -> PluginExecutionResult:
    """验证账号存在公开比赛后覆盖当前渠道用户唯一绑定。"""

    if not raw_id:
        return _error_result("命令格式：注册Dota 86745912", "account_id_required")
    account_id = _normalize_account_id(raw_id)
    player = _request_json(config, f"players/{account_id}")
    matches = _request_json(config, f"players/{account_id}/recentMatches")
    if not isinstance(player, Mapping):
        raise Dota2StatsError("OpenDota 玩家资料格式异常。")
    if not isinstance(matches, list) or not matches:
        raise Dota2StatsError("这个账号没有可公开查询的最近比赛。")
    profile = player.get("profile") if isinstance(player.get("profile"), Mapping) else {}
    player_name = str(profile.get("personaname") or f"Dota 玩家 {account_id}").strip()
    store.put(
        USER_BINDING_NAMESPACE,
        _user_binding_state_key(trusted.sender_id),
        {
            "user_id": trusted.sender_id,
            "account_id": account_id,
            "original_id": str(raw_id).strip(),
            "player_name": player_name,
            "updated_at": now_shanghai().isoformat(),
        },
    )
    return _text_result(
        f"Dota 2 账号绑定成功\n玩家：{player_name}\nAccount ID：{account_id}\n以后发送“{QUERY_COMMAND}”即可查询。",
        "registered",
        account_id=account_id,
    )


def _handle_query(
    store: PluginStateStore,
    *,
    config: Dota2Config,
    trusted: TrustedDotaMessage,
    raw_id: str,
) -> PluginExecutionResult:
    """查询显式账号或当前用户绑定，并生成图片或同源文本回复。"""

    if raw_id:
        account_id = _normalize_account_id(raw_id)
    else:
        binding = store.get(USER_BINDING_NAMESPACE, _user_binding_state_key(trusted.sender_id))
        if binding is None:
            return _error_result(
                f"你还没有绑定 Dota 2 账号，请先发送：{REGISTER_COMMAND} 86745912",
                "account_not_bound",
            )
        account_id = _normalize_account_id(binding.get("account_id"))

    report = _load_or_build_personal_report(store, config=config, account_id=account_id)
    replies, reply_type = _build_report_replies(
        markdown=_build_personal_markdown(report),
        fallback_text=_build_personal_text(report),
        render_as_image=config.render_as_image,
        image_prefix="dota2-personal",
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已查询 {report.get('player_name')} 的 Dota 2 最近战绩。",
        output={
            "provider_status": "completed",
            "account_id": account_id,
            "match_count": int((report.get("summary") or {}).get("match_count") or 0),
            "reply_type": reply_type,
            "replies": replies,
        },
    )


def _handle_group_ranking(
    store: PluginStateStore,
    *,
    config: Dota2Config,
    trusted: TrustedDotaMessage,
) -> PluginExecutionResult:
    """普通群成员只读取当天快照，命令链路不允许现场访问 OpenDota。"""

    if str(trusted.conversation.chat_type or "").strip().lower() != "group":
        return _error_result("Dota群排行仅支持在群聊中查看。", "group_required")
    stat_date = now_shanghai().date().isoformat()
    report = store.get(
        GROUP_SNAPSHOT_NAMESPACE,
        _group_snapshot_key(int(trusted.conversation.id), stat_date),
        now=now_shanghai(),
    )
    if report is None:
        return _error_result("今天的 Dota 群排行尚未生成，或本群暂时没有成员绑定账号。", "snapshot_missing")
    replies, reply_type = _build_report_replies(
        markdown=_build_group_markdown(report),
        fallback_text=_build_group_text(report),
        render_as_image=config.render_as_image,
        image_prefix="dota2-group",
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已读取 {stat_date} 的 Dota 2 群排行。",
        output={
            "provider_status": "snapshot",
            "stat_date": stat_date,
            "reply_type": reply_type,
            "replies": replies,
        },
    )


def _load_or_build_personal_report(
    store: PluginStateStore,
    *,
    config: Dota2Config,
    account_id: int,
) -> dict[str, Any]:
    """短期复用已清洗个人报告，缓存中不保存 API Key 或第三方完整响应。"""

    cache_key = f"account-{account_id}"
    cached = store.get(PERSONAL_CACHE_NAMESPACE, cache_key, now=now_shanghai())
    if cached is not None and int(cached.get("account_id") or 0) == account_id:
        return cached

    player = _request_json(config, f"players/{account_id}")
    matches = _request_json(config, f"players/{account_id}/recentMatches")
    win_loss = _request_json(config, f"players/{account_id}/wl")
    if not isinstance(player, Mapping) or not isinstance(matches, list) or not isinstance(win_loss, Mapping):
        raise Dota2StatsError("OpenDota 战绩数据格式异常。")
    if not matches:
        raise Dota2StatsError("这个账号没有可公开查询的最近比赛。")
    hero_names = _load_hero_names(store, config)
    report = _build_personal_report(
        account_id=account_id,
        player=player,
        matches=[dict(item) for item in matches if isinstance(item, Mapping)],
        win_loss=win_loss,
        hero_names=hero_names,
        config=config,
    )
    store.put(
        PERSONAL_CACHE_NAMESPACE,
        cache_key,
        report,
        ttl_seconds=config.dynamic_cache_seconds,
        now=now_shanghai(),
    )
    return report


def _load_hero_names(store: PluginStateStore, config: Dota2Config) -> dict[str, str]:
    """把英雄常量压缩为 ID 到名称的小映射，避免完整第三方对象超过状态容量。"""

    cached = store.get(HERO_NAMES_NAMESPACE, "current", now=now_shanghai())
    if cached is not None and isinstance(cached.get("names"), Mapping):
        return {str(key): str(value) for key, value in cached["names"].items() if str(value).strip()}
    payload = _request_json(config, "constants/heroes")
    if not isinstance(payload, Mapping):
        raise Dota2StatsError("OpenDota 英雄常量格式异常。")
    names = {
        str(hero_id): str(hero.get("localized_name") or "").strip()
        for hero_id, hero in payload.items()
        if isinstance(hero, Mapping) and str(hero.get("localized_name") or "").strip()
    }
    store.put(
        HERO_NAMES_NAMESPACE,
        "current",
        {"names": names, "updated_at": now_shanghai().isoformat()},
        ttl_seconds=7 * 24 * 60 * 60,
        now=now_shanghai(),
    )
    return names


def _build_personal_report(
    *,
    account_id: int,
    player: Mapping[str, Any],
    matches: list[dict[str, Any]],
    win_loss: Mapping[str, Any],
    hero_names: Mapping[str, str],
    config: Dota2Config,
) -> dict[str, Any]:
    """按来源插件口径计算近期胜负、KDA、生涯统计和逐场详情。"""

    selected = matches[: config.summary_match_limit]
    normalized_matches: list[dict[str, Any]] = []
    for match in selected:
        hero_id = _safe_int(match.get("hero_id"))
        normalized_matches.append(
            {
                "match_id": _safe_int(match.get("match_id")),
                "hero_id": hero_id,
                "hero_name": hero_names.get(str(hero_id)) or f"英雄 {hero_id}",
                "won": _did_player_win(match),
                "kills": _safe_int(match.get("kills")),
                "deaths": _safe_int(match.get("deaths")),
                "assists": _safe_int(match.get("assists")),
                "kda": _match_kda(match),
                "duration": _format_duration(match.get("duration")),
                "start_time": _format_start_time(match.get("start_time")),
                "game_mode": _format_game_mode(match),
                "gpm": _safe_int(match.get("gold_per_min")),
                "xpm": _safe_int(match.get("xp_per_min")),
            }
        )
    wins = sum(bool(item["won"]) for item in normalized_matches)
    losses = len(normalized_matches) - wins
    total_kills = sum(int(item["kills"]) for item in normalized_matches)
    total_deaths = sum(int(item["deaths"]) for item in normalized_matches)
    total_assists = sum(int(item["assists"]) for item in normalized_matches)
    lifetime_wins = max(_safe_int(win_loss.get("win")), 0)
    lifetime_losses = max(_safe_int(win_loss.get("lose")), 0)
    lifetime_matches = lifetime_wins + lifetime_losses
    profile = player.get("profile") if isinstance(player.get("profile"), Mapping) else {}
    rank_tier = _safe_int(player.get("rank_tier"))
    return {
        "account_id": account_id,
        "player_name": str(profile.get("personaname") or f"Dota 玩家 {account_id}").strip(),
        "rank_tier": rank_tier,
        "rank_name": _format_rank_tier(rank_tier),
        "leaderboard_rank": _safe_int(player.get("leaderboard_rank")),
        "summary": {
            "match_count": len(normalized_matches),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(normalized_matches) * 100, 1) if normalized_matches else 0.0,
            "aggregate_kda": round((total_kills + total_assists) / max(total_deaths, 1), 2),
            "lifetime_wins": lifetime_wins,
            "lifetime_losses": lifetime_losses,
            "lifetime_match_count": lifetime_matches,
            "lifetime_win_rate": round(lifetime_wins / lifetime_matches * 100, 1) if lifetime_matches else 0.0,
            "detail_matches": normalized_matches[: config.recent_match_limit],
        },
        "generated_at": now_shanghai().isoformat(),
    }


def _prepare_group_snapshot(
    db: Session,
    *,
    store: PluginStateStore,
    config: Dota2Config,
    conversation: Conversation,
    stat_date: str,
) -> PluginExecutionResult:
    """读取 SaaS 群成员绑定，按 Account ID 去重采集并保存当天群榜快照。"""

    members = _load_group_members(db, conversation, config.max_group_players)
    if not members:
        return _skipped_group_result(stat_date, int(conversation.id), "当前群没有可参与排行的成员。")

    bound_members: list[tuple[ChatroomMember, dict[str, Any]]] = []
    for member in members:
        user_id = str(member.member_external_id or "").strip()
        state = store.get(USER_BINDING_NAMESPACE, _user_binding_state_key(user_id)) if user_id else None
        if state is None:
            continue
        account_id = _safe_int(state.get("account_id"))
        if account_id > 0:
            bound_members.append((member, {**state, "account_id": account_id}))
    if not bound_members:
        return _skipped_group_result(stat_date, int(conversation.id), "当前群没有成员绑定 Dota 2 账号。")

    account_stats: dict[int, dict[str, Any]] = {}
    for account_id in sorted({int(binding["account_id"]) for _member, binding in bound_members}):
        account_stats[account_id] = _load_or_fetch_group_account_stat(
            store,
            config=config,
            account_id=account_id,
            stat_date=stat_date,
        )

    rows: list[dict[str, Any]] = []
    for member, binding in bound_members:
        user_id = str(member.member_external_id or "").strip()
        account_id = int(binding["account_id"])
        stat = account_stats[account_id]
        rows.append(
            {
                **stat,
                "user_id": user_id,
                "display_name": str(member.display_name or member.nickname or user_id).strip() or user_id,
            }
        )
    report = _build_group_report(
        conversation=conversation,
        stat_date=stat_date,
        rows=rows,
        config=config,
    )
    store.put(
        GROUP_SNAPSHOT_NAMESPACE,
        _group_snapshot_key(int(conversation.id), stat_date),
        report,
        ttl_seconds=config.snapshot_retention_days * 24 * 60 * 60,
        now=now_shanghai(),
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已生成 {stat_date} 的 Dota 2 群排行快照。",
        output={
            "stat_date": stat_date,
            "conversation_id": int(conversation.id),
            "bound_user_count": len(bound_members),
            "unique_account_count": len(account_stats),
            "success_count": int(report["success_count"]),
            "error_count": int(report["error_count"]),
        },
    )


def _load_or_fetch_group_account_stat(
    store: PluginStateStore,
    *,
    config: Dota2Config,
    account_id: int,
    stat_date: str,
) -> dict[str, Any]:
    """同一绑定内按账号和日期复用采集结果，跨群任务不会重复请求相同账号。"""

    state_key = f"{stat_date}:{account_id}"
    cached = store.get(GROUP_ACCOUNT_NAMESPACE, state_key, now=now_shanghai())
    if cached is not None and int(cached.get("account_id") or 0) == account_id:
        return cached
    try:
        player = _request_json(config, f"players/{account_id}")
        matches = _request_json(
            config,
            f"players/{account_id}/matches",
            params={"date": config.group_ranking_window_days},
        )
        if not isinstance(player, Mapping) or not isinstance(matches, list):
            raise Dota2StatsError("OpenDota 群排行数据格式异常。")
        stat = _build_group_member_stat(
            account_id=account_id,
            player=player,
            matches=[dict(item) for item in matches if isinstance(item, Mapping)],
            stat_date=stat_date,
            window_days=config.group_ranking_window_days,
        )
    except (PluginHttpError, Dota2StatsError, ValueError):
        stat = {
            "account_id": account_id,
            "player_name": f"Dota 玩家 {account_id}",
            "rank_tier": 0,
            "rank_name": "未公开",
            "leaderboard_rank": 0,
            "match_count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "daily_results": _empty_daily_results(stat_date, config.group_ranking_window_days),
            "fetch_status": "error",
        }
    store.put(
        GROUP_ACCOUNT_NAMESPACE,
        state_key,
        stat,
        ttl_seconds=2 * 24 * 60 * 60,
        now=now_shanghai(),
    )
    return stat


def _build_group_member_stat(
    *,
    account_id: int,
    player: Mapping[str, Any],
    matches: list[dict[str, Any]],
    stat_date: str,
    window_days: int,
) -> dict[str, Any]:
    """把公开比赛压缩为上海自然日窗口内的单成员统计。"""

    end_date = date.fromisoformat(stat_date)
    start_date = end_date - timedelta(days=window_days - 1)
    daily_map = {
        (start_date + timedelta(days=index)).isoformat(): {
            "date": (start_date + timedelta(days=index)).isoformat(),
            "wins": 0,
            "losses": 0,
            "total": 0,
        }
        for index in range(window_days)
    }
    seen_match_ids: set[int] = set()
    for match in matches:
        match_id = _safe_int(match.get("match_id"))
        if match_id > 0 and match_id in seen_match_ids:
            continue
        if match_id > 0:
            seen_match_ids.add(match_id)
        try:
            match_date = ensure_shanghai_datetime(
                datetime.fromtimestamp(_safe_int(match.get("start_time")), tz=now_shanghai().tzinfo)
            ).date()
        except (OSError, OverflowError, ValueError):
            continue
        if match_date < start_date or match_date > end_date:
            continue
        bucket = daily_map[match_date.isoformat()]
        if _did_player_win(match):
            bucket["wins"] += 1
        else:
            bucket["losses"] += 1
        bucket["total"] += 1
    wins = sum(int(item["wins"]) for item in daily_map.values())
    losses = sum(int(item["losses"]) for item in daily_map.values())
    match_count = wins + losses
    profile = player.get("profile") if isinstance(player.get("profile"), Mapping) else {}
    rank_tier = _safe_int(player.get("rank_tier"))
    return {
        "account_id": account_id,
        "player_name": str(profile.get("personaname") or f"Dota 玩家 {account_id}").strip(),
        "rank_tier": rank_tier,
        "rank_name": _format_rank_tier(rank_tier),
        "leaderboard_rank": _safe_int(player.get("leaderboard_rank")),
        "match_count": match_count,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / match_count * 100, 1) if match_count else 0.0,
        "daily_results": list(daily_map.values()),
        "fetch_status": "ok",
    }


def _build_group_report(
    *,
    conversation: Conversation,
    stat_date: str,
    rows: list[dict[str, Any]],
    config: Dota2Config,
) -> dict[str, Any]:
    """生成群概览以及场次、胜率和段位三类稳定排行榜。"""

    success_rows = [row for row in rows if row.get("fetch_status") == "ok"]
    total_matches = sum(int(row.get("match_count") or 0) for row in success_rows)
    total_wins = sum(int(row.get("wins") or 0) for row in success_rows)
    attendance = sorted(
        success_rows,
        key=lambda row: (-int(row.get("match_count") or 0), -int(row.get("wins") or 0), str(row.get("display_name") or "")),
    )
    win_rate = sorted(
        [row for row in success_rows if int(row.get("match_count") or 0) >= config.minimum_win_rate_matches],
        key=lambda row: (-float(row.get("win_rate") or 0), -int(row.get("match_count") or 0), str(row.get("display_name") or "")),
    )
    rank_tier = sorted(
        [row for row in success_rows if int(row.get("rank_tier") or 0) > 0],
        key=lambda row: (-int(row.get("rank_tier") or 0), int(row.get("leaderboard_rank") or 999999), str(row.get("display_name") or "")),
    )

    def positioned(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**item, "position": index} for index, item in enumerate(items[: config.group_ranking_limit], start=1)]

    return {
        "conversation_id": int(conversation.id),
        "group_name": str(conversation.title or conversation.external_id or "Dota 2 群").strip(),
        "stat_date": stat_date,
        "window_days": config.group_ranking_window_days,
        "bound_count": len(rows),
        "success_count": len(success_rows),
        "error_count": len(rows) - len(success_rows),
        "total_matches": total_matches,
        "total_wins": total_wins,
        "total_losses": total_matches - total_wins,
        "group_win_rate": round(total_wins / total_matches * 100, 1) if total_matches else 0.0,
        "minimum_win_rate_matches": config.minimum_win_rate_matches,
        "attendance_ranking": positioned(attendance),
        "win_rate_ranking": positioned(win_rate),
        "rank_tier_ranking": positioned(rank_tier),
        "generated_at": now_shanghai().isoformat(),
    }


def _push_group_snapshot(
    db: Session,
    *,
    store: PluginStateStore,
    config: Dota2Config,
    binding: BotPlugin,
    conversation: Conversation,
    channel: Channel,
    stat_date: str,
) -> PluginExecutionResult:
    """从当天快照生成标准出站任务，整个路径不调用 OpenDota。"""

    report = store.get(
        GROUP_SNAPSHOT_NAMESPACE,
        _group_snapshot_key(int(conversation.id), stat_date),
        now=now_shanghai(),
    )
    if report is None:
        return _skipped_group_result(stat_date, int(conversation.id), "当天 Dota 2 群排行快照尚未生成。")
    dedupe_key = f"dota2-ranking:{int(binding.id)}:{int(conversation.id)}:{stat_date}:push"
    existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
    if existing is not None:
        return _group_push_result(report, conversation=conversation, job=existing, reply_type="existing")

    payload, reply_type = _build_group_outbound_payload(conversation, report, config)
    try:
        with db.begin_nested():
            job = enqueue_outbound_job(
                db,
                conversation=conversation,
                channel=channel,
                payload=payload,
                plugin_code=PLUGIN_CODE,
                related_type="dota2_stats_binding",
                related_id=int(binding.id),
                priority=70,
                dedupe_key=dedupe_key,
                max_attempts=3,
            )
    except IntegrityError:
        job = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
        if job is None:
            raise
    return _group_push_result(report, conversation=conversation, job=job, reply_type=reply_type)


def _build_group_outbound_payload(
    conversation: Conversation,
    report: Mapping[str, Any],
    config: Dota2Config,
) -> tuple[dict[str, Any], str]:
    """把群榜图片或文本回复转换为当前群聊的通用出站载荷。"""

    replies, reply_type = _build_report_replies(
        markdown=_build_group_markdown(report),
        fallback_text=_build_group_text(report),
        render_as_image=config.render_as_image,
        image_prefix="dota2-group",
    )
    reply = replies[0]
    base = {
        "external_id": str(conversation.external_id or "").strip(),
        "receiver_id": str(conversation.external_id or "").strip(),
        "chat_type": "group",
    }
    if reply_type == "image":
        return {**base, "message_type": "image", "base64": str(reply.get("base64") or "")}, "image"
    return {**base, "message_type": "text", "content": str(reply.get("content") or "")}, "text"


def _build_report_replies(
    *,
    markdown: str,
    fallback_text: str,
    render_as_image: bool,
    image_prefix: str,
) -> tuple[list[dict[str, Any]], str]:
    """临时生成图片并立即转 base64，文件校验和清理规则与其他报告插件一致。"""

    if render_as_image:
        image_path: Path | None = None
        try:
            output_name = f"{image_prefix}-{uuid.uuid4().hex}.png"
            image_path = Path(
                convert_md_str_to_image_sync(markdown, output_name, base_dir=PROJECT_ROOT)
            ).resolve(strict=True)
            image_path.relative_to(DOTA_IMAGE_ROOT.resolve())
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            return standard_replies([image_reply(base64_data=encoded)]), "image"
        except Exception:
            pass
        finally:
            if image_path is not None:
                try:
                    image_path.relative_to(DOTA_IMAGE_ROOT.resolve())
                    image_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
    return standard_replies([text_reply(fallback_text)]), "text"


def _build_personal_text(report: Mapping[str, Any]) -> str:
    """使用清洗后的同源报告生成个人战绩文本兜底。"""

    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Dota 2 最近战绩｜{report.get('player_name') or '未知玩家'}",
        f"Account ID：{int(report.get('account_id') or 0)}｜段位：{report.get('rank_name') or '未公开'}",
        (
            f"近{int(summary.get('match_count') or 0)}场：{int(summary.get('wins') or 0)}胜 "
            f"{int(summary.get('losses') or 0)}负｜胜率 {float(summary.get('win_rate') or 0):.1f}%｜"
            f"KDA {float(summary.get('aggregate_kda') or 0):.2f}"
        ),
        (
            f"生涯{int(summary.get('lifetime_match_count') or 0)}场："
            f"{int(summary.get('lifetime_wins') or 0)}胜 {int(summary.get('lifetime_losses') or 0)}负｜"
            f"胜率 {float(summary.get('lifetime_win_rate') or 0):.1f}%"
        ),
        "",
    ]
    for index, match in enumerate(summary.get("detail_matches") or [], start=1):
        if not isinstance(match, Mapping):
            continue
        lines.append(
            f"{index}. {'胜利' if match.get('won') else '失败'}｜{match.get('hero_name')}｜"
            f"{int(match.get('kills') or 0)}/{int(match.get('deaths') or 0)}/{int(match.get('assists') or 0)}｜"
            f"{match.get('duration')}｜GPM {int(match.get('gpm') or 0)}｜{match.get('start_time')}"
        )
    return "\n".join(lines).strip()


def _build_personal_markdown(report: Mapping[str, Any]) -> str:
    """生成适合现有 Markdown 转图能力的个人战绩版式。"""

    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"# {_markdown_cell(report.get('player_name') or '未知玩家')} Dota 2 战绩",
        "",
        f"> Account ID {int(report.get('account_id') or 0)} | 段位 {report.get('rank_name') or '未公开'}",
        "",
        "| 近期场次 | 胜 / 负 | 胜率 | KDA | 生涯场次 |",
        "| ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {int(summary.get('match_count') or 0)} | {int(summary.get('wins') or 0)} / "
            f"{int(summary.get('losses') or 0)} | {float(summary.get('win_rate') or 0):.1f}% | "
            f"{float(summary.get('aggregate_kda') or 0):.2f} | {int(summary.get('lifetime_match_count') or 0)} |"
        ),
        "",
        "## 最近比赛",
        "",
        "| 结果 | 英雄 | K / D / A | 时长 | GPM | 时间 |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for match in summary.get("detail_matches") or []:
        if not isinstance(match, Mapping):
            continue
        lines.append(
            f"| {'胜利' if match.get('won') else '失败'} | {_markdown_cell(match.get('hero_name'))} | "
            f"{int(match.get('kills') or 0)} / {int(match.get('deaths') or 0)} / {int(match.get('assists') or 0)} | "
            f"{match.get('duration')} | {int(match.get('gpm') or 0)} | {match.get('start_time')} |"
        )
    return "\n".join(lines)


def _build_group_text(report: Mapping[str, Any]) -> str:
    """生成群榜命令和定时推送共用的业务文本。"""

    lines = [
        f"{report.get('group_name') or 'Dota 2 群'} Dota 2 群排行",
        f"{report.get('stat_date')}｜近 {int(report.get('window_days') or 7)} 天",
        (
            f"绑定 {int(report.get('bound_count') or 0)} 人｜比赛 {int(report.get('total_matches') or 0)} 场｜"
            f"群胜率 {float(report.get('group_win_rate') or 0):.1f}%"
        ),
        "",
        "场次排行",
    ]
    for row in report.get("attendance_ranking") or []:
        if isinstance(row, Mapping):
            lines.append(
                f"{int(row.get('position') or 0)}. {row.get('display_name')}："
                f"{int(row.get('match_count') or 0)} 场，{int(row.get('wins') or 0)} 胜"
            )
    lines.extend(["", "胜率排行"])
    for row in report.get("win_rate_ranking") or []:
        if isinstance(row, Mapping):
            lines.append(
                f"{int(row.get('position') or 0)}. {row.get('display_name')}："
                f"{float(row.get('win_rate') or 0):.1f}%（{int(row.get('match_count') or 0)} 场）"
            )
    lines.extend(["", "段位排行"])
    for row in report.get("rank_tier_ranking") or []:
        if isinstance(row, Mapping):
            lines.append(f"{int(row.get('position') or 0)}. {row.get('display_name')}：{row.get('rank_name')}")
    return "\n".join(lines).strip()


def _build_group_markdown(report: Mapping[str, Any]) -> str:
    """生成三榜并列的群排行 Markdown。"""

    lines = [
        f"# {_markdown_cell(report.get('group_name') or 'Dota 2 群')} Dota 2 群排行",
        "",
        f"> {report.get('stat_date')} | 近 {int(report.get('window_days') or 7)} 天 | "
        f"绑定 {int(report.get('bound_count') or 0)} 人 | 比赛 {int(report.get('total_matches') or 0)} 场 | "
        f"群胜率 {float(report.get('group_win_rate') or 0):.1f}%",
    ]
    sections = (
        ("场次排行", "attendance_ranking", "比赛", lambda row: f"{int(row.get('match_count') or 0)} 场"),
        ("胜率排行", "win_rate_ranking", "胜率", lambda row: f"{float(row.get('win_rate') or 0):.1f}%"),
        ("段位排行", "rank_tier_ranking", "段位", lambda row: str(row.get("rank_name") or "未公开")),
    )
    for title, key, metric_label, formatter in sections:
        lines.extend(["", f"## {title}", "", f"| 排名 | 成员 | {metric_label} |", "| ---: | --- | ---: |"])
        for row in report.get(key) or []:
            if isinstance(row, Mapping):
                lines.append(
                    f"| {int(row.get('position') or 0)} | {_markdown_cell(row.get('display_name'))} | {formatter(row)} |"
                )
    return "\n".join(lines)


def _request_json(
    config: Dota2Config,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
) -> Any:
    """只访问固定 OpenDota 官方地址，并统一执行超时、响应上限和幂等重试。"""

    query = {str(key): str(value) for key, value in dict(params or {}).items() if str(value).strip()}
    if config.api_key:
        query["api_key"] = config.api_key
    url = f"{OPEN_DOTA_API_BASE}/{str(path or '').strip('/')}"
    if query:
        url = f"{url}?{urlencode(query)}"
    response = PluginHttpClient().request(
        "GET",
        url,
        headers={"Accept": "application/json", "User-Agent": "fulcrum/dota2-stats"},
        connect_timeout_seconds=min(5, config.request_timeout_seconds),
        total_timeout_seconds=config.request_timeout_seconds,
        max_response_bytes=4 * 1024 * 1024,
        max_redirects=2,
        retry_count=config.retry_count,
    )
    if response.status_code == 404:
        raise Dota2StatsError("OpenDota 未找到该玩家或公开数据。")
    if response.status_code == 429:
        raise Dota2StatsError("OpenDota 请求过于频繁，请稍后重试。")
    if response.status_code < 200 or response.status_code >= 300:
        raise Dota2StatsError("OpenDota 服务暂时不可用，请稍后重试。")
    try:
        return response.json()
    except (TypeError, ValueError) as exc:
        raise Dota2StatsError("OpenDota 返回了无法解析的数据。") from exc


def _require_trusted_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedDotaMessage | None, PluginExecutionResult | None]:
    """验证消息、发送者、会话、渠道、机器人和插件绑定属于同一租户链路。"""

    if context is None or context.message_id is None or context.conversation_id is None or context.channel_id is None:
        return None, _error_result("Dota 2 战绩需要可信入站消息。", "trusted_context_required")
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
        return None, _error_result("Dota 2 战绩绑定或机器人未启用。", "binding_unavailable")
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    if (
        channel is None
        or not channel.is_enabled
        or int(channel.bot_id) != int(binding.bot_id)
        or conversation is None
        or int(conversation.bot_id) != int(binding.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or message is None
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip().lower() != "in"
    ):
        return None, _error_result("Dota 2 战绩消息归属无效。", "message_scope_invalid")
    sender_id = str(message.sender_id or "").strip()
    if not sender_id or (context.sender_id is not None and sender_id != str(context.sender_id).strip()):
        return None, _error_result("Dota 2 战绩发送者身份无效。", "sender_invalid")
    return (
        TrustedDotaMessage(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            message=message,
            sender_id=sender_id,
            sender_name=str(message.sender_name or sender_id).strip() or sender_id,
        ),
        None,
    )


def _require_scheduled_group(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[BotPlugin | None, Conversation | None, Channel | None, PluginExecutionResult | None]:
    """验证定时任务只操作当前绑定明确指定的群会话。"""

    if context is None or context.conversation_id is None:
        return None, None, None, _error_result("Dota 2 群排行缺少目标群聊。", "target_required")
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
        return None, None, None, _error_result("Dota 2 群排行绑定或机器人未启用。", "binding_unavailable")
    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.bot_id) != int(binding.bot_id)
        or str(conversation.chat_type or "").strip().lower() != "group"
        or conversation.channel_id is None
    ):
        return binding, None, None, _error_result("Dota 2 群排行目标不是当前机器人的群聊。", "invalid_target")
    channel = db.get(Channel, int(conversation.channel_id))
    if channel is None or not channel.is_enabled or int(channel.bot_id) != int(binding.bot_id):
        return binding, conversation, None, _error_result("Dota 2 群排行目标渠道未启用。", "channel_unavailable")
    return binding, conversation, channel, None


def _load_group_members(db: Session, conversation: Conversation, limit: int) -> list[ChatroomMember]:
    """通过 SaaS 群和成员表读取稳定成员身份，不依赖 Provider 联系人缓存。"""

    if conversation.channel_id is None:
        return []
    chatroom = db.scalar(
        select(Chatroom)
        .where(
            Chatroom.bot_id == int(conversation.bot_id),
            Chatroom.channel_id == int(conversation.channel_id),
            Chatroom.external_id == str(conversation.external_id or ""),
        )
        .order_by(Chatroom.id.asc())
        .limit(1)
    )
    if chatroom is None:
        return []
    return list(
        db.scalars(
            select(ChatroomMember)
            .where(
                ChatroomMember.chatroom_id == int(chatroom.id),
                ChatroomMember.status != "left",
            )
            .order_by(ChatroomMember.id.asc())
            .limit(limit)
        ).all()
    )


def _parse_command(content: str) -> tuple[str, str]:
    """按完整命令匹配，并只放宽 Dota 英文字母大小写。"""

    normalized = " ".join(str(content or "").strip().split())
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    folded = normalized.casefold()
    if folded == GROUP_RANKING_COMMAND.casefold():
        return "group_ranking", ""
    for action, command in (("register", REGISTER_COMMAND), ("query", QUERY_COMMAND)):
        if folded == command.casefold():
            return action, ""
        prefix = f"{command} "
        if folded.startswith(prefix.casefold()):
            return action, normalized[len(prefix) :].strip()
    return "", ""


def _normalize_account_id(raw_id: Any) -> int:
    """把好友码、Account ID 或 SteamID64 收敛为无符号 32 位 account_id。"""

    text = str(raw_id or "").strip()
    if not text or not text.isdigit():
        raise Dota2StatsError("Dota ID 必须是纯数字好友码、Account ID 或 SteamID64。")
    account_id = int(text)
    if account_id >= STEAM_ID64_BASE:
        account_id -= STEAM_ID64_BASE
    if account_id <= 0 or account_id > MAX_ACCOUNT_ID:
        raise Dota2StatsError("Dota ID 超出有效范围，请检查后重试。")
    return account_id


def _did_player_win(match: Mapping[str, Any]) -> bool:
    is_radiant = _safe_int(match.get("player_slot")) < 128
    radiant_win = bool(match.get("radiant_win"))
    return radiant_win if is_radiant else not radiant_win


def _match_kda(match: Mapping[str, Any]) -> float:
    return round(
        (_safe_int(match.get("kills")) + _safe_int(match.get("assists")))
        / max(_safe_int(match.get("deaths")), 1),
        2,
    )


def _format_duration(value: Any) -> str:
    total_seconds = max(_safe_int(value), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _format_start_time(value: Any) -> str:
    timestamp = _safe_int(value)
    if timestamp <= 0:
        return "时间未知"
    try:
        return ensure_shanghai_datetime(datetime.fromtimestamp(timestamp, tz=now_shanghai().tzinfo)).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (OSError, OverflowError, ValueError):
        return "时间未知"


def _format_rank_tier(value: Any) -> str:
    rank_value = _safe_int(value)
    medal = rank_value // 10
    star = rank_value % 10
    name = RANK_NAMES.get(medal)
    if not name:
        return "未公开"
    return name if medal == 8 or star <= 0 else f"{name} {star}星"


def _format_game_mode(match: Mapping[str, Any]) -> str:
    mode = GAME_MODE_NAMES.get(_safe_int(match.get("game_mode")), "普通匹配")
    return f"天梯 · {mode}" if _safe_int(match.get("lobby_type")) == 7 else mode


def _empty_daily_results(stat_date: str, window_days: int) -> list[dict[str, Any]]:
    end_date = date.fromisoformat(stat_date)
    start_date = end_date - timedelta(days=window_days - 1)
    return [
        {
            "date": (start_date + timedelta(days=index)).isoformat(),
            "wins": 0,
            "losses": 0,
            "total": 0,
        }
        for index in range(window_days)
    ]


def _user_binding_state_key(user_id: str) -> str:
    digest = hashlib.sha256(str(user_id or "").strip().encode("utf-8")).hexdigest()
    return f"user:{digest}"


def _group_snapshot_key(conversation_id: int, stat_date: str) -> str:
    return f"conversation-{int(conversation_id)}:{stat_date}"


def _group_push_result(
    report: Mapping[str, Any],
    *,
    conversation: Conversation,
    job: TaskJob,
    reply_type: str,
) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已为 {report.get('stat_date')} 的 Dota 2 群排行创建推送任务。",
        output={
            "stat_date": str(report.get("stat_date") or ""),
            "conversation_id": int(conversation.id),
            "bound_user_count": int(report.get("bound_count") or 0),
            "reply_type": reply_type,
            "outbound_job_id": int(job.id),
        },
    )


def _skipped_group_result(stat_date: str, conversation_id: int, message: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="skipped",
        message=message,
        output={"stat_date": stat_date, "conversation_id": int(conversation_id)},
    )


def _text_result(text: str, provider_status: str, **output: Any) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=text.splitlines()[0],
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(text)]),
            **output,
        },
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )


def _markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def _load_config(raw: Mapping[str, Any]) -> Dota2Config:
    """读取唯一配置路径，OpenDota 地址固定为官方接口，不接受租户覆盖。"""

    return Dota2Config(
        api_key=str(raw.get("api_key") or "").strip(),
        request_timeout_seconds=_bounded_int(raw.get("request_timeout_seconds"), default=15, minimum=5, maximum=60),
        retry_count=_bounded_int(raw.get("retry_count"), default=2, minimum=0, maximum=2),
        user_requests_per_minute=_bounded_int(raw.get("user_requests_per_minute"), default=2, minimum=1, maximum=30),
        dynamic_cache_seconds=_bounded_int(raw.get("dynamic_cache_seconds"), default=120, minimum=30, maximum=3600),
        summary_match_limit=_bounded_int(raw.get("summary_match_limit"), default=20, minimum=1, maximum=50),
        recent_match_limit=_bounded_int(raw.get("recent_match_limit"), default=10, minimum=1, maximum=20),
        render_as_image=_as_bool(raw.get("render_as_image"), default=True),
        group_ranking_window_days=_bounded_int(raw.get("group_ranking_window_days"), default=7, minimum=1, maximum=30),
        group_ranking_limit=_bounded_int(raw.get("group_ranking_limit"), default=10, minimum=3, maximum=20),
        minimum_win_rate_matches=_bounded_int(raw.get("minimum_win_rate_matches"), default=3, minimum=1, maximum=30),
        max_group_players=_bounded_int(raw.get("max_group_players"), default=50, minimum=1, maximum=100),
        snapshot_retention_days=_bounded_int(raw.get("snapshot_retention_days"), default=30, minimum=1, maximum=365),
    )
