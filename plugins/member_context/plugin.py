from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.plugin_runtime.state_store import PluginStateStore


PLUGIN_CODE = "member_context"
PROFILE_NAMESPACE = "member_context_profiles"
DAILY_ACTION = "member_context_daily_refresh"
WEEKLY_ACTION = "member_context_weekly_refresh"
MONTHLY_ACTION = "member_context_monthly_refresh"
SUPPORTED_ACTIONS = {DAILY_ACTION, WEEKLY_ACTION, MONTHLY_ACTION}
MAX_PROFILE_TEXT_LENGTH = 500
MAX_PROFILE_LIST_ITEMS = 8
MAX_PROFILE_ITEM_LENGTH = 80
MAX_MESSAGE_SAMPLE_LENGTH = 240
CHINESE_TEXT_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,16}")
LATIN_TEXT_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,31}")
QUESTION_PATTERN = re.compile(r"[?？]|(?:怎么|如何|为什么|是否|能否|可以吗|行不行)")
STOP_TOPICS = {
    "这个",
    "那个",
    "就是",
    "然后",
    "什么",
    "怎么",
    "一下",
    "一个",
    "我们",
    "你们",
    "他们",
    "现在",
    "已经",
    "还是",
    "可以",
    "需要",
    "问题",
}


@dataclass(frozen=True)
class MemberContextConfig:
    """保存一个租户插件绑定唯一生效的成员画像参数。"""

    api_base: str
    api_key: str
    request_timeout_seconds: int
    sample_days: int
    incremental_message_limit: int
    minimum_member_messages: int
    ai_min_member_messages: int
    max_members_per_run: int
    max_group_messages: int


@dataclass(frozen=True)
class TrustedGroupTarget:
    """保存调度器可信上下文交叉验证后的群聊定位。"""

    binding: BotPlugin
    conversation: Conversation
    channel: Channel
    chatroom: Chatroom


@dataclass(frozen=True)
class MemberCandidate:
    """保存一个成员本次真正新增的消息及其旧画像。"""

    member_id: str
    display_name: str
    messages: tuple[Message, ...]
    previous_profile: Mapping[str, Any]
    state_key: str


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """提供不访问数据库的运行状态入口，实际画像只能由可信调度任务生成。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群成员画像插件已就绪。",
            output={
                "provider_configured": bool(config.api_base and config.api_key),
                "schedule_mode": "scheduled_only",
            },
        )
    if request.action == "execute":
        return PluginExecutionResult(
            ok=True,
            status="ready",
            message="群成员画像将按已选择群聊的日、周、月计划自动刷新。",
            output={"schedule_mode": "scheduled_only"},
        )
    return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """读取一个可信群聊的增量消息，先构建本地画像，再可选使用 Dify 批量增强。"""

    if request.action not in SUPPORTED_ACTIONS:
        return PluginExecutionResult(
            ok=False,
            status="unsupported",
            message=f"不支持的群成员画像动作: {request.action}",
        )
    config = _load_config(request.config)

    target, error = _require_trusted_group_target(db, request)
    if error is not None or target is None:
        return error or _result(
            ok=False,
            status="error",
            message="群成员画像缺少可信群聊上下文。",
            provider_status="trusted_context_required",
        )

    context = request.context
    if context is None:
        return _result(
            ok=False,
            status="error",
            message="群成员画像缺少可信运行上下文。",
            provider_status="trusted_context_required",
        )
    store = PluginStateStore(db, context)
    candidates = _load_member_candidates(db, target=target, store=store, config=config)
    if not candidates:
        return _result(
            ok=True,
            status="skipped",
            message="当前群聊没有需要增量刷新的成员画像。",
            provider_status="local",
            conversation_id=int(target.conversation.id),
            refreshed_member_count=0,
            ai_profile_count=0,
        )

    # 本地画像始终先生成。即使租户没有配置 Dify、上游超时或返回越权成员，
    # 水位和基础画像仍可稳定推进，不会让付费外部能力成为主流程单点。
    local_profiles = {
        candidate.member_id: _build_local_profile(target=target, candidate=candidate)
        for candidate in candidates
    }
    provider_status = "local"
    ai_profile_count = 0
    if config.api_base and config.api_key:
        ai_profiles = _request_dify_profiles(
            request,
            config=config,
            target=target,
            candidates=candidates,
        )
        if ai_profiles is None:
            provider_status = "fallback"
        else:
            provider_status = "completed"
            for member_id, ai_profile in ai_profiles.items():
                local = local_profiles.get(member_id)
                if local is None:
                    # Dify 返回成员必须属于本次候选白名单；任何跨群或伪造成员结果
                    # 都直接丢弃，不能借助模型输出修改其他成员的状态记录。
                    continue
                local_profiles[member_id] = _merge_ai_profile(local, ai_profile)
                ai_profile_count += 1

    for candidate in candidates:
        store.put(PROFILE_NAMESPACE, candidate.state_key, local_profiles[candidate.member_id])

    return _result(
        ok=True,
        status="completed",
        message=f"已刷新 {len(candidates)} 位群成员画像。",
        provider_status=provider_status,
        conversation_id=int(target.conversation.id),
        refreshed_member_count=len(candidates),
        ai_profile_count=ai_profile_count,
    )


def _require_trusted_group_target(
    db: Session,
    request: PluginExecutionRequest,
) -> tuple[TrustedGroupTarget | None, PluginExecutionResult | None]:
    """交叉验证租户、绑定、机器人、渠道、会话和群记录，拒绝业务载荷伪造身份。"""

    context = request.context
    if context is None or context.conversation_id is None:
        return None, _result(
            ok=False,
            status="error",
            message="群成员画像任务缺少目标群聊。",
            provider_status="target_required",
        )
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
        return None, _result(
            ok=False,
            status="error",
            message="群成员画像绑定或机器人未启用。",
            provider_status="binding_unavailable",
        )

    conversation = db.get(Conversation, int(context.conversation_id))
    if (
        conversation is None
        or int(conversation.bot_id) != int(binding.bot_id)
        or str(conversation.chat_type or "").strip().lower() != "group"
        or conversation.channel_id is None
    ):
        return None, _result(
            ok=False,
            status="error",
            message="群成员画像目标不是当前机器人的群聊。",
            provider_status="invalid_target",
        )
    if context.channel_id is not None and int(conversation.channel_id) != int(context.channel_id):
        return None, _result(
            ok=False,
            status="error",
            message="群成员画像目标渠道不一致。",
            provider_status="invalid_channel",
        )

    channel = db.get(Channel, int(conversation.channel_id))
    if channel is None or not channel.is_enabled or int(channel.bot_id) != int(binding.bot_id):
        return None, _result(
            ok=False,
            status="error",
            message="群成员画像目标渠道未启用。",
            provider_status="channel_unavailable",
        )
    chatroom = db.scalar(
        select(Chatroom)
        .where(
            Chatroom.bot_id == int(binding.bot_id),
            Chatroom.channel_id == int(channel.id),
            Chatroom.external_id == str(conversation.external_id or ""),
            Chatroom.is_active.is_(True),
        )
        .order_by(Chatroom.id.asc())
        .limit(1)
    )
    if chatroom is None:
        return None, _result(
            ok=False,
            status="error",
            message="群成员画像目标群尚未完成成员同步。",
            provider_status="chatroom_unavailable",
        )
    return TrustedGroupTarget(binding, conversation, channel, chatroom), None


def _load_member_candidates(
    db: Session,
    *,
    target: TrustedGroupTarget,
    store: PluginStateStore,
    config: MemberContextConfig,
) -> tuple[MemberCandidate, ...]:
    """按群消息水位筛选本次候选成员，首次建档与后续增量使用同一条数据路径。"""

    cutoff = now_shanghai() - timedelta(days=config.sample_days)
    recent_messages = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == int(target.conversation.id),
            Message.direction == "in",
            Message.message_type == "text",
            Message.sender_id.is_not(None),
            Message.content != "",
            Message.created_at >= cutoff,
        )
        .order_by(Message.id.desc())
        .limit(config.max_group_messages)
    ).all()
    if not recent_messages:
        return ()

    active_members = db.scalars(
        select(ChatroomMember).where(
            ChatroomMember.chatroom_id == int(target.chatroom.id),
            ChatroomMember.status != "left",
        )
    ).all()
    member_map = {
        str(member.member_external_id or "").strip(): member
        for member in active_members
        if str(member.member_external_id or "").strip()
    }
    grouped: dict[str, list[Message]] = {}
    for message in reversed(recent_messages):
        member_id = str(message.sender_id or "").strip()
        if member_id in member_map:
            grouped.setdefault(member_id, []).append(message)

    candidates: list[MemberCandidate] = []
    for member_id, messages in grouped.items():
        state_key = _profile_state_key(target, member_id)
        previous = store.get(PROFILE_NAMESPACE, state_key, now=now_shanghai()) or {}
        watermark = _safe_int(previous.get("last_message_id"), default=0)
        incremental = tuple(
            message
            for message in messages
            if int(message.id or 0) > watermark
        )[-config.incremental_message_limit :]
        # 首次建档要求达到最小样本量；已有画像只要出现一条新消息就推进，避免低频
        # 成员长期无法更新，也保证重复任务在水位不变时自然跳过。
        if not incremental or (not previous and len(incremental) < config.minimum_member_messages):
            continue
        member = member_map[member_id]
        display_name = str(member.display_name or member.nickname or member_id).strip() or member_id
        candidates.append(
            MemberCandidate(
                member_id=member_id,
                display_name=display_name,
                messages=incremental,
                previous_profile=previous,
                state_key=state_key,
            )
        )

    candidates.sort(
        key=lambda item: (-int(item.messages[-1].id or 0), item.member_id),
    )
    return tuple(candidates[: config.max_members_per_run])


def _build_local_profile(
    *,
    target: TrustedGroupTarget,
    candidate: MemberCandidate,
) -> dict[str, Any]:
    """从有限增量文本推导可解释的基础画像，并与旧画像做去重合并。"""

    texts = [_normalize_message_text(message.content) for message in candidate.messages]
    texts = [text for text in texts if text]
    previous = dict(candidate.previous_profile)
    new_topics = _extract_topics(texts)
    topics = _merge_text_items(previous.get("topics_of_interest"), new_topics, limit=6)
    recent_focus = _merge_text_items(new_topics, previous.get("recent_focus"), limit=4)
    total_count = _safe_int(previous.get("source_message_count_total"), default=0) + len(candidate.messages)
    question_count = sum(1 for text in texts if QUESTION_PATTERN.search(text))
    average_length = round(sum(len(text) for text in texts) / max(len(texts), 1), 1)
    previous_meta = previous.get("meta") if isinstance(previous.get("meta"), Mapping) else {}
    profile_iterations = _safe_int(previous_meta.get("profile_iterations"), default=0) + 1

    return {
        "member_id": candidate.member_id,
        "display_name": candidate.display_name,
        "channel_id": int(target.channel.id),
        "chatroom_id": int(target.chatroom.id),
        "conversation_id": int(target.conversation.id),
        "activity_level": _activity_level(total_count),
        "message_pattern": _message_pattern(average_length, question_count, len(texts)),
        "interaction_style": _interaction_style(question_count, len(texts)),
        "response_style_hint": _response_style_hint(question_count, len(texts)),
        "topics_of_interest": topics,
        "recent_focus": recent_focus,
        "summary_text": _local_summary(candidate.display_name, topics, question_count, len(texts)),
        "confidence": min(0.9, round(0.35 + min(total_count, 30) * 0.015, 2)),
        "source_message_count": len(candidate.messages),
        "source_message_count_total": total_count,
        "last_message_id": max(int(message.id or 0) for message in candidate.messages),
        "last_profiled_at": now_shanghai().isoformat(),
        "meta": {
            **dict(previous_meta),
            "profile_iterations": profile_iterations,
            "question_ratio": round(question_count / max(len(texts), 1), 3),
            "average_message_length": average_length,
            "group_role": str(previous_meta.get("group_role") or "").strip(),
        },
    }


def _request_dify_profiles(
    request: PluginExecutionRequest,
    *,
    config: MemberContextConfig,
    target: TrustedGroupTarget,
    candidates: Sequence[MemberCandidate],
) -> dict[str, Mapping[str, Any]] | None:
    """按群一次性调用 Dify，并只返回本次候选白名单内的结构化结果。"""

    ai_candidates = [
        candidate
        for candidate in candidates
        if _safe_int(candidate.previous_profile.get("source_message_count_total"), default=0)
        + len(candidate.messages)
        >= config.ai_min_member_messages
    ]
    if not ai_candidates:
        return {}

    # 请求只包含有限长度的本次增量消息和最小旧画像提示，不读取原始请求字段，也不
    # 把请求正文写入插件状态。租户密钥仅存在于本次受控 HTTP 请求头中。
    members = []
    for candidate in ai_candidates:
        previous = candidate.previous_profile
        members.append(
            {
                "member_id": candidate.member_id,
                "display_name": candidate.display_name,
                "recent_messages": [
                    _normalize_message_text(message.content)[:MAX_MESSAGE_SAMPLE_LENGTH]
                    for message in candidate.messages
                ],
                "previous_summary": str(previous.get("summary_text") or "")[:MAX_PROFILE_TEXT_LENGTH],
                "previous_topics": _normalize_text_items(previous.get("topics_of_interest"), limit=6),
            }
        )
    context = request.context
    stable_user = (
        f"tenant-{context.tenant_id}:binding-{context.binding_id}:conversation-{context.conversation_id}"
        if context is not None
        else "member-context"
    )
    prompt = (
        "请为 members_json 中的群成员生成结构化画像。只能返回输入中的 member_id，"
        "不得推断职业、家庭、健康、政治、宗教等敏感信息。输出 JSON 对象，根字段为 members；"
        "每个成员可包含 topics、skill_profile、problem_solving_profile、value_profile、"
        "expression_profile、reply_entry_profile、group_role、decision_profile、summary_text、confidence。"
    )
    payload = {
        "inputs": {
            "query": prompt,
            "group_name": str(target.conversation.title or target.chatroom.name or "群聊"),
            "members_json": json.dumps(members, ensure_ascii=False, separators=(",", ":")),
        },
        "response_mode": "blocking",
        "user": stable_user,
    }
    try:
        response = PluginHttpClient().post_json(
            f"{config.api_base}/workflows/run",
            payload,
            headers={"Authorization": f"Bearer {config.api_key}"},
            connect_timeout_seconds=min(10, config.request_timeout_seconds),
            total_timeout_seconds=config.request_timeout_seconds,
            max_response_bytes=2 * 1024 * 1024,
            max_redirects=2,
        )
        if response.status_code < 200 or response.status_code >= 300:
            return None
        parsed = _extract_dify_members(response.json())
    except Exception:
        # 外部画像增强不是主流程。网络、超时、JSON 或工作流契约错误均回退本地结果，
        # 但不吞掉数据库写入错误，因为数据库写入发生在本函数返回之后。
        return None

    allowed_ids = {candidate.member_id for candidate in ai_candidates}
    return {
        member_id: profile
        for member_id, profile in parsed.items()
        if member_id in allowed_ids
    }


def _extract_dify_members(payload: Any) -> dict[str, Mapping[str, Any]]:
    """只接受 Dify workflow 的 `data.outputs.text` JSON 契约。"""

    if not isinstance(payload, Mapping):
        raise ValueError("Dify 响应不是对象")
    data = payload.get("data")
    outputs = data.get("outputs") if isinstance(data, Mapping) else None
    text = outputs.get("text") if isinstance(outputs, Mapping) else None
    normalized = str(text or "").strip()
    normalized = re.sub(r"^```(?:json)?\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*```$", "", normalized).strip()
    result = json.loads(normalized)
    if not isinstance(result, Mapping) or not isinstance(result.get("members"), list):
        raise ValueError("Dify 成员画像结果格式不正确")
    profiles: dict[str, Mapping[str, Any]] = {}
    for item in result["members"]:
        if not isinstance(item, Mapping):
            continue
        member_id = str(item.get("member_id") or "").strip()
        if member_id:
            profiles[member_id] = item
    return profiles


def _merge_ai_profile(local: Mapping[str, Any], ai: Mapping[str, Any]) -> dict[str, Any]:
    """把白名单字段合并进本地画像，身份、水位和统计字段始终由平台保留。"""

    merged = dict(local)
    topics = _normalize_text_items(ai.get("topics"), limit=6)
    if topics:
        merged["topics_of_interest"] = topics
        merged["recent_focus"] = topics[:4]
    summary = _normalize_profile_text(ai.get("summary_text"))
    if summary:
        merged["summary_text"] = summary
    confidence = _safe_float(ai.get("confidence"), default=float(merged.get("confidence") or 0.0))
    merged["confidence"] = max(0.0, min(confidence, 1.0))

    meta = dict(merged.get("meta") if isinstance(merged.get("meta"), Mapping) else {})
    list_fields = (
        "skill_profile",
        "problem_solving_profile",
        "value_profile",
        "expression_profile",
        "reply_entry_profile",
    )
    for field in list_fields:
        values = _normalize_text_items(ai.get(field), limit=MAX_PROFILE_LIST_ITEMS)
        if values:
            meta[field] = values
    for field in ("group_role", "decision_profile"):
        value = _normalize_profile_text(ai.get(field), limit=MAX_PROFILE_ITEM_LENGTH)
        if value:
            meta[field] = value
    merged["meta"] = meta
    return merged


def _extract_topics(texts: Sequence[str]) -> list[str]:
    """从本次增量文本提取有限主题词，为未配置模型的租户提供基础可用画像。"""

    counter: Counter[str] = Counter()
    for text in texts:
        # 中文没有可靠空格边界，这里保留短语前十二个字符；英文和数字标识按词提取。
        # 该逻辑只生成提示性主题，不尝试替代租户可选的结构化模型能力。
        for raw in CHINESE_TEXT_PATTERN.findall(text):
            topic = raw[:12]
            if topic not in STOP_TOPICS:
                counter[topic] += 1
        for raw in LATIN_TEXT_PATTERN.findall(text):
            topic = raw.lower()
            if topic not in STOP_TOPICS:
                counter[topic] += 1
    return [topic for topic, _ in counter.most_common(6)] or ["群聊交流"]


def _profile_state_key(target: TrustedGroupTarget, member_id: str) -> str:
    """使用可信渠道、群主键和成员哈希生成稳定且不泄露外部账号的状态键。"""

    member_hash = hashlib.sha256(member_id.encode("utf-8")).hexdigest()[:24]
    return f"channel-{int(target.channel.id)}:chatroom-{int(target.chatroom.id)}:member-{member_hash}"


def _normalize_message_text(value: Any) -> str:
    """压缩消息空白并限制单条样本长度，避免异常长文本放大外部调用成本。"""

    return re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_MESSAGE_SAMPLE_LENGTH]


def _normalize_text_items(value: Any, *, limit: int) -> list[str]:
    """把模型或旧画像列表收敛为短文本去重列表。"""

    if not isinstance(value, (list, tuple)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _normalize_profile_text(item, limit=MAX_PROFILE_ITEM_LENGTH)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if len(normalized) >= limit:
            break
    return normalized


def _merge_text_items(first: Any, second: Any, *, limit: int) -> list[str]:
    """按新旧优先级合并画像条目，不让重复刷新无限增长状态。"""

    values: list[str] = []
    for source in (first, second):
        for item in _normalize_text_items(source, limit=limit):
            if item not in values:
                values.append(item)
            if len(values) >= limit:
                return values
    return values


def _normalize_profile_text(value: Any, *, limit: int = MAX_PROFILE_TEXT_LENGTH) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _activity_level(total_count: int) -> str:
    if total_count >= 30:
        return "高活跃"
    if total_count >= 10:
        return "稳定活跃"
    return "持续观察"


def _message_pattern(average_length: float, question_count: int, message_count: int) -> str:
    length_label = "偏详细" if average_length >= 30 else "偏简洁"
    question_label = "，常通过提问推进讨论" if question_count / max(message_count, 1) >= 0.4 else ""
    return f"表达{length_label}{question_label}"


def _interaction_style(question_count: int, message_count: int) -> str:
    if question_count / max(message_count, 1) >= 0.4:
        return "倾向先确认信息，再参与讨论"
    return "倾向直接表达观点并推动话题"


def _response_style_hint(question_count: int, message_count: int) -> str:
    if question_count / max(message_count, 1) >= 0.4:
        return "先给明确结论，再补充验证依据"
    return "围绕当前主题直接回应，避免无关延伸"


def _local_summary(display_name: str, topics: Sequence[str], question_count: int, message_count: int) -> str:
    topic_text = "、".join(topics[:3]) or "群聊交流"
    style = "经常通过提问确认细节" if question_count / max(message_count, 1) >= 0.4 else "倾向直接表达和推进讨论"
    return f"{display_name}近期关注{topic_text}，{style}。"


def _load_config(raw: Mapping[str, Any]) -> MemberContextConfig:
    """限制采样规模和外部调用时限，防止单个租户任务无限扩大资源消耗。"""

    return MemberContextConfig(
        api_base=str(raw.get("api_base") or "").strip().rstrip("/"),
        api_key=str(raw.get("api_key") or "").strip(),
        request_timeout_seconds=_bounded_int(
            raw.get("request_timeout_seconds"), default=60, minimum=10, maximum=180
        ),
        sample_days=_bounded_int(raw.get("sample_days"), default=30, minimum=1, maximum=180),
        incremental_message_limit=_bounded_int(
            raw.get("incremental_message_limit"), default=80, minimum=1, maximum=500
        ),
        minimum_member_messages=_bounded_int(
            raw.get("minimum_member_messages"), default=2, minimum=1, maximum=50
        ),
        ai_min_member_messages=_bounded_int(
            raw.get("ai_min_member_messages"), default=2, minimum=1, maximum=100
        ),
        max_members_per_run=_bounded_int(
            raw.get("max_members_per_run"), default=30, minimum=1, maximum=100
        ),
        max_group_messages=_bounded_int(
            raw.get("max_group_messages"), default=600, minimum=20, maximum=5000
        ),
    )


def _result(
    *,
    ok: bool,
    status: str,
    message: str,
    provider_status: str,
    **output: Any,
) -> PluginExecutionResult:
    """统一成员画像动作输出，避免向任务结果写入消息样本或租户密钥。"""

    return PluginExecutionResult(
        ok=ok,
        status=status,
        message=message,
        output={"provider_status": provider_status, **output},
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
