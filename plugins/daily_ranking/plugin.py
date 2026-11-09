from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.markdown_to_image import convert_md_str_to_image_sync
from app.core.time import now_shanghai
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
from app.services.plugin_runtime.state_store import PluginStateStore
from app.services.points import PointsService


PLUGIN_CODE = "daily_ranking"
PREPARE_ACTION = "daily_ranking_prepare"
PUSH_ACTION = "daily_ranking_push"
REPORT_NAMESPACE = "daily_ranking_reports"
REPORT_TTL_SECONDS = 3 * 24 * 60 * 60
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RANKING_IMAGE_ROOT = PROJECT_ROOT / "data" / "temp" / "md2image"


@dataclass(frozen=True)
class DailyRankingConfig:
    """保存一个插件绑定唯一生效的日榜配置。"""

    display_limit: int
    reward_limit: int
    render_as_image: bool


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """提供后台运行状态，不开放缺少数据库可信上下文的人工排行入口。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群聊每日排行插件运行正常。",
            output={
                "display_limit": config.display_limit,
                "reward_limit": config.reward_limit,
                "render_as_image": config.render_as_image,
            },
        )
    if request.action == "execute":
        return PluginExecutionResult(
            ok=True,
            status="ready",
            message="群聊每日排行已就绪，将按目标群的定时配置自动执行。",
            output={
                "schedule_mode": "scheduled_only",
            },
        )
    return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """为一个可信目标群预生成昨日排行，或发放积分后创建统一出站任务。"""

    if request.action not in {PREPARE_ACTION, PUSH_ACTION}:
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的群聊每日排行动作: {request.action}",
        )
    binding, conversation, channel, error = _require_scheduled_target(db, request)
    if error is not None or binding is None or conversation is None or channel is None:
        return error or _error_result("群聊每日排行缺少可信运行上下文。", "trusted_context_required")

    config = _load_config(request.config)

    context = request.context
    if context is None:
        return _error_result("群聊每日排行缺少可信运行上下文。", "trusted_context_required")
    current = now_shanghai()
    date_key, start_at, end_at = _resolve_yesterday(current)
    state_key = _report_state_key(int(conversation.id), date_key)
    store = PluginStateStore(db, context)

    if request.action == PREPARE_ACTION:
        report = _build_report(
            db,
            conversation=conversation,
            start_at=start_at,
            end_at=end_at,
            date_key=date_key,
            config=config,
        )
        if report is None:
            return _skipped_result(date_key, int(conversation.id))
        store.put(
            REPORT_NAMESPACE,
            state_key,
            report,
            ttl_seconds=REPORT_TTL_SECONDS,
            now=current,
        )
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=f"已预生成 {date_key} 的群聊每日排行。",
            output={
                "date_key": date_key,
                "conversation_id": int(conversation.id),
                "message_count": int(report["total_messages"]),
                "participant_count": int(report["participant_count"]),
                "cache_status": "prepared",
            },
        )

    cached = store.get(REPORT_NAMESPACE, state_key, now=current)
    report = _validate_cached_report(cached, conversation_id=int(conversation.id), date_key=date_key)
    cache_hit = report is not None
    if report is None:
        report = _build_report(
            db,
            conversation=conversation,
            start_at=start_at,
            end_at=end_at,
            date_key=date_key,
            config=config,
        )
        if report is None:
            return _skipped_result(date_key, int(conversation.id))
        store.put(
            REPORT_NAMESPACE,
            state_key,
            report,
            ttl_seconds=REPORT_TTL_SECONDS,
            now=current,
        )

    delivery_key = _delivery_dedupe_key(binding, conversation, date_key)
    existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == delivery_key).limit(1))
    if existing is not None:
        return _push_result(
            report,
            conversation=conversation,
            job=existing,
            cache_hit=cache_hit,
            reply_type="existing",
        )

    # 图片渲染先于积分写入执行，避免渲染环境故障影响奖励结果。渲染失败会直接
    # 生成同内容文本载荷，后续奖励和出站仍沿用同一条业务路径。
    outbound_payload, reply_type = _build_outbound_payload(conversation, report, config)
    _award_report_points(db, binding=binding, conversation=conversation, report=report)
    job = _enqueue_report(
        db,
        binding=binding,
        conversation=conversation,
        channel=channel,
        payload=outbound_payload,
        dedupe_key=delivery_key,
    )
    return _push_result(
        report,
        conversation=conversation,
        job=job,
        cache_hit=cache_hit,
        reply_type=reply_type,
    )


def _require_scheduled_target(
    db: Session,
    request: PluginExecutionRequest,
) -> tuple[BotPlugin | None, Conversation | None, Channel | None, PluginExecutionResult | None]:
    """交叉验证租户、绑定、机器人、群会话和渠道均来自调度器可信上下文。"""

    context = request.context
    if context is None or context.conversation_id is None:
        return None, None, None, _error_result("群聊每日排行缺少目标群聊。", "target_required")
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
        return None, None, None, _error_result("群聊每日排行绑定或机器人未启用。", "binding_unavailable")

    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.bot_id) != int(binding.bot_id)
        or str(conversation.chat_type or "").strip().lower() != "group"
        or conversation.channel_id is None
    ):
        return binding, None, None, _error_result("群聊每日排行目标不是当前机器人的群聊。", "invalid_target")
    if context.channel_id is not None and int(conversation.channel_id) != int(context.channel_id):
        return binding, None, None, _error_result("群聊每日排行目标渠道不一致。", "invalid_channel")

    channel = db.get(Channel, int(conversation.channel_id))
    if channel is None or not channel.is_enabled or int(channel.bot_id) != int(binding.bot_id):
        return binding, conversation, None, _error_result("群聊每日排行目标渠道未启用。", "channel_unavailable")
    return binding, conversation, channel, None


def _build_report(
    db: Session,
    *,
    conversation: Conversation,
    start_at: datetime,
    end_at: datetime,
    date_key: str,
    config: DailyRankingConfig,
) -> dict[str, Any] | None:
    """从 SaaS 消息和群成员表构建一份不含渠道原始数据的轻量排行。"""

    base_filters = (
        Message.conversation_id == int(conversation.id),
        Message.direction == "in",
        Message.created_at >= start_at,
        Message.created_at < end_at,
        Message.sender_id.is_not(None),
        func.trim(Message.sender_id) != "",
    )
    total_messages, participant_count = db.execute(
        select(func.count(Message.id), func.count(func.distinct(Message.sender_id))).where(*base_filters)
    ).one()
    if int(total_messages or 0) <= 0:
        return None

    ranking_rows = db.execute(
        select(
            Message.sender_id,
            func.count(Message.id).label("message_count"),
            func.max(Message.sender_name).label("fallback_name"),
        )
        .where(*base_filters)
        .group_by(Message.sender_id)
        .order_by(func.count(Message.id).desc(), Message.sender_id.asc())
        .limit(config.reward_limit)
    ).all()
    sender_ids = [str(row.sender_id or "").strip() for row in ranking_rows if str(row.sender_id or "").strip()]
    member_names, member_count = _load_member_names(db, conversation, sender_ids)

    rows: list[dict[str, Any]] = []
    for rank, row in enumerate(ranking_rows, start=1):
        user_id = str(row.sender_id or "").strip()
        if not user_id:
            continue
        message_count = int(row.message_count or 0)
        fallback_name = str(row.fallback_name or "").strip()
        display_name = member_names.get(user_id) or fallback_name or user_id
        rows.append(
            {
                "rank": rank,
                "user_id": user_id,
                "display_name": display_name,
                "message_count": message_count,
                "reward_points": _reward_for_rank(rank),
                "contribution_percent": round(message_count / max(int(total_messages), 1) * 100, 1),
            }
        )

    return {
        "date_key": date_key,
        "conversation_id": int(conversation.id),
        "conversation_title": str(conversation.title or conversation.external_id or "群聊").strip() or "群聊",
        "total_messages": int(total_messages),
        "participant_count": int(participant_count or 0),
        "member_count": member_count,
        "display_limit": config.display_limit,
        "rows": rows,
    }


def _load_member_names(
    db: Session,
    conversation: Conversation,
    sender_ids: list[str],
) -> tuple[dict[str, str], int]:
    """按当前渠道和会话外部编号定位群成员，不读取任何 Provider 联系人缓存。"""

    if conversation.channel_id is None:
        return {}, 0
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
        return {}, 0

    member_count = int(
        db.scalar(
            select(func.count(ChatroomMember.id)).where(
                ChatroomMember.chatroom_id == int(chatroom.id),
                ChatroomMember.status != "left",
            )
        )
        or 0
    )
    if not sender_ids:
        return {}, member_count
    members = db.scalars(
        select(ChatroomMember).where(
            ChatroomMember.chatroom_id == int(chatroom.id),
            ChatroomMember.member_external_id.in_(sender_ids),
            ChatroomMember.status != "left",
        )
    ).all()
    names = {
        str(member.member_external_id): str(member.display_name or member.nickname or "").strip()
        for member in members
        if str(member.display_name or member.nickname or "").strip()
    }
    return names, member_count


def _award_report_points(
    db: Session,
    *,
    binding: BotPlugin,
    conversation: Conversation,
    report: Mapping[str, Any],
) -> None:
    """按绑定、会话、日期、名次和用户生成稳定键，确保任务重跑不重复奖励。"""

    group_id = str(conversation.external_id or "").strip()
    date_key = str(report.get("date_key") or "").strip()
    service = PointsService(db)
    for raw_row in report.get("rows", []):
        if not isinstance(raw_row, Mapping):
            continue
        user_id = str(raw_row.get("user_id") or "").strip()
        rank = _bounded_int(raw_row.get("rank"), default=0, minimum=0, maximum=100)
        reward = _bounded_int(raw_row.get("reward_points"), default=0, minimum=0, maximum=1000)
        if not user_id or rank <= 0 or reward <= 0:
            continue
        result = service.add_points(
            user_id,
            group_id,
            reward,
            user_name=str(raw_row.get("display_name") or user_id).strip() or user_id,
            source=PLUGIN_CODE,
            description=f"{date_key} 群聊活跃榜第 {rank} 名奖励",
            metadata={
                "binding_id": int(binding.id),
                "conversation_id": int(conversation.id),
                "date_key": date_key,
                "rank": rank,
            },
            idempotency_key=(
                f"daily-ranking:{int(binding.id)}:{int(conversation.id)}:{date_key}:{rank}:{user_id}"
            ),
        )
        if not result.ok:
            raise RuntimeError("群聊每日排行积分发放失败")


def _build_outbound_payload(
    conversation: Conversation,
    report: Mapping[str, Any],
    config: DailyRankingConfig,
) -> tuple[dict[str, Any], str]:
    """优先生成临时图片并立即转为 base64；任何渲染失败都使用业务文本兜底。"""

    base_payload = {
        "external_id": str(conversation.external_id or "").strip(),
        "receiver_id": str(conversation.external_id or "").strip(),
        "chat_type": "group",
    }
    if config.render_as_image:
        image_path: Path | None = None
        try:
            output_name = f"daily-ranking-{uuid.uuid4().hex}.png"
            image_path = Path(
                convert_md_str_to_image_sync(
                    _build_report_markdown(report),
                    output_name,
                    base_dir=PROJECT_ROOT,
                )
            ).resolve(strict=True)
            # 只允许读取平台 Markdown 转图目录中的临时文件，防止插件配置或异常返回
            # 把服务器任意文件转换为可外发内容。
            image_path.relative_to(RANKING_IMAGE_ROOT.resolve())
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            return {**base_payload, "message_type": "image", "base64": encoded}, "image"
        except Exception:
            pass
        finally:
            if image_path is not None:
                try:
                    image_path.relative_to(RANKING_IMAGE_ROOT.resolve())
                    image_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
    return {**base_payload, "message_type": "text", "content": _build_report_text(report)}, "text"


def _enqueue_report(
    db: Session,
    *,
    binding: BotPlugin,
    conversation: Conversation,
    channel: Channel,
    payload: dict[str, Any],
    dedupe_key: str,
) -> TaskJob:
    """使用租户任务唯一键为同一自然日的同一群聊只创建一个出站任务。"""

    existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
    if existing is not None:
        return existing
    try:
        with db.begin_nested():
            job = enqueue_outbound_job(
                db,
                conversation=conversation,
                channel=channel,
                payload=payload,
                plugin_code=PLUGIN_CODE,
                related_type="daily_ranking_binding",
                related_id=int(binding.id),
                priority=70,
                dedupe_key=dedupe_key,
                max_attempts=3,
            )
        return job
    except IntegrityError:
        existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key).limit(1))
        if existing is None:
            raise
        return existing


def _build_report_markdown(report: Mapping[str, Any]) -> str:
    """生成紧凑的渠道无关日榜 Markdown，图片和文本共享同一结构化数据。"""

    title = str(report.get("conversation_title") or "群聊").strip() or "群聊"
    date_key = str(report.get("date_key") or "").strip()
    display_rows = _display_rows(report)
    member_count = int(report.get("member_count") or 0)
    member_text = str(member_count) if member_count > 0 else "--"
    lines = [
        f"# {_escape_markdown_cell(title)} 每日活跃榜",
        "",
        f"> {date_key} | 总消息 {int(report.get('total_messages') or 0)} | "
        f"参与用户 {int(report.get('participant_count') or 0)} | 群成员 {member_text}",
        "",
        "| 排名 | 成员 | 发言数 | 占比 | 奖励 |",
        "| ---: | --- | ---: | ---: | ---: |",
    ]
    for row in display_rows:
        lines.append(
            f"| {int(row.get('rank') or 0)} | {_escape_markdown_cell(row.get('display_name'))} | "
            f"{int(row.get('message_count') or 0)} | {float(row.get('contribution_percent') or 0):.1f}% | "
            f"{int(row.get('reward_points') or 0)} 积分 |"
        )
    lines.extend(["", f"前 {len(report.get('rows', []))} 名积分奖励已按自然日幂等发放。"])
    return "\n".join(lines)


def _build_report_text(report: Mapping[str, Any]) -> str:
    """生成图片不可用时直接面向群成员的完整业务文本。"""

    date_key = str(report.get("date_key") or "").strip()
    title = str(report.get("conversation_title") or "群聊").strip() or "群聊"
    lines = [
        f"{date_key} 群聊活跃榜",
        f"群聊：{title}",
        f"总消息 {int(report.get('total_messages') or 0)} | 参与用户 {int(report.get('participant_count') or 0)}",
        "",
    ]
    for row in _display_rows(report):
        lines.append(
            f"{int(row.get('rank') or 0)}. {str(row.get('display_name') or '').strip()}："
            f"{int(row.get('message_count') or 0)} 条，奖励 {int(row.get('reward_points') or 0)} 积分"
        )
    return "\n".join(lines).strip()


def _display_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """限制展示人数，但保留报告中的完整奖励名单用于积分发放。"""

    limit = _bounded_int(report.get("display_limit"), default=10, minimum=1, maximum=20)
    rows = report.get("rows")
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:limit] if isinstance(row, Mapping)]


def _validate_cached_report(
    value: Mapping[str, Any] | None,
    *,
    conversation_id: int,
    date_key: str,
) -> dict[str, Any] | None:
    """缓存必须精确属于当前会话和自然日，损坏状态直接重新计算。"""

    if not isinstance(value, Mapping):
        return None
    if int(value.get("conversation_id") or 0) != int(conversation_id):
        return None
    if str(value.get("date_key") or "").strip() != date_key:
        return None
    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    return dict(value)


def _push_result(
    report: Mapping[str, Any],
    *,
    conversation: Conversation,
    job: TaskJob,
    cache_hit: bool,
    reply_type: str,
) -> PluginExecutionResult:
    """返回不含图片、用户名列表和敏感载荷的定时任务摘要。"""

    return PluginExecutionResult(
        ok=True,
        status="completed",
        message=f"已为 {report.get('date_key')} 的群聊每日排行创建推送任务。",
        output={
            "date_key": str(report.get("date_key") or ""),
            "conversation_id": int(conversation.id),
            "message_count": int(report.get("total_messages") or 0),
            "participant_count": int(report.get("participant_count") or 0),
            "rewarded_user_count": len(report.get("rows", [])),
            "cache_hit": cache_hit,
            "reply_type": reply_type,
            "outbound_job_id": int(job.id),
        },
    )


def _skipped_result(date_key: str, conversation_id: int) -> PluginExecutionResult:
    """昨日无有效入站消息时正常跳过，不把空榜视为任务故障。"""

    return PluginExecutionResult(
        ok=True,
        status="skipped",
        message=f"{date_key} 没有可统计的群聊消息，已跳过每日排行。",
        output={
            "date_key": date_key,
            "conversation_id": int(conversation_id),
            "message_count": 0,
            "participant_count": 0,
        },
    )


def _resolve_yesterday(current: datetime) -> tuple[str, datetime, datetime]:
    """按服务器业务时区计算完整的上一自然日半开区间。"""

    yesterday = current.date() - timedelta(days=1)
    start_at = datetime.combine(yesterday, time.min, tzinfo=current.tzinfo)
    return yesterday.isoformat(), start_at, start_at + timedelta(days=1)


def _report_state_key(conversation_id: int, date_key: str) -> str:
    return f"conversation-{int(conversation_id)}:{date_key}"


def _delivery_dedupe_key(binding: BotPlugin, conversation: Conversation, date_key: str) -> str:
    return f"daily-ranking:{int(binding.id)}:{int(conversation.id)}:{date_key}:push"


def _reward_for_rank(rank: int) -> int:
    """保持来源插件现有积分规则，不把奖励金额扩展为可随意修改的租户字段。"""

    if rank == 1:
        return 30
    if rank == 2:
        return 20
    if rank == 3:
        return 10
    if rank <= 10:
        return 5
    return 3


def _load_config(raw_config: Mapping[str, Any]) -> DailyRankingConfig:
    """收敛日榜人数边界，奖励人数必须覆盖图片展示人数。"""

    display_limit = _bounded_int(raw_config.get("display_limit"), default=10, minimum=1, maximum=20)
    reward_limit = _bounded_int(raw_config.get("reward_limit"), default=20, minimum=1, maximum=100)
    return DailyRankingConfig(
        display_limit=display_limit,
        reward_limit=max(display_limit, reward_limit),
        render_as_image=_as_bool(raw_config.get("render_as_image"), default=True),
    )


def _escape_markdown_cell(value: Any) -> str:
    """避免成员名称破坏 Markdown 表格结构。"""

    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


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


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={"provider_status": provider_status},
    )
