from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    ChatroomMember,
    Conversation,
    Message,
    Plugin,
    PluginStateRecord,
)
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import parse_state_payload
from app.services.plugin_runtime.text_generation import PluginTextGenerationClient, PluginTextGenerationError


PLUGIN_CODE = "member_roast"
PROFILE_PLUGIN_CODE = "member_context"
PROFILE_NAMESPACE = "member_context_profiles"
SELF_COMMANDS = {"锐评我", "锐评一下我", "吐槽我", "吐槽一下我"}
COMMAND_PREFIXES = ("锐评一下", "吐槽一下", "锐评", "吐槽")
MAX_PROMPT_TEXT_LENGTH = 500
MAX_PROFILE_LIST_ITEMS = 6
MAX_REPEATED_PHRASES = 5
QUESTION_PATTERN = re.compile(r"[?？]|(?:怎么|如何|为什么|是否|能否|可以吗)")
EXCLAMATION_PATTERN = re.compile(r"[!！]")
TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]{1,31}|[\u4e00-\u9fff]{2,12}")


@dataclass(frozen=True)
class MemberRoastConfig:
    """保存当前租户绑定唯一生效的模型、采样、限流和输出配置。"""

    llm_api_base: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    temperature: float
    max_tokens: int
    sample_days: int
    message_limit: int
    min_message_count: int
    representative_message_limit: int
    user_rate_limit: int
    group_rate_limit: int
    rate_window_seconds: int
    max_output_chars: int


@dataclass(frozen=True)
class TrustedRoastMessage:
    """保存经过数据库交叉验证的锐评消息及当前群定位。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    chatroom: Chatroom
    sender: ChatroomMember
    message: Message


@dataclass(frozen=True)
class RoastTarget:
    """保存当前群内唯一解析出的被锐评成员。"""

    member_id: str
    display_name: str


def matches_event(context) -> dict[str, object]:
    """只匹配群聊文本中的当前锐评命令，不接受普通消息隐式触发模型。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "text").strip().lower()
    content = str(getattr(context, "content", "") or "")
    command, _target = _parse_command(content)
    matched = chat_type == "group" and message_type == "text" and bool(command)
    return {
        "matched": matched,
        "reason": "member_roast_hit" if matched else "member_roast_command_required",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只报告配置状态，锐评业务必须来自一条真实群入站消息。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="群成员锐评插件已就绪。",
            output={
                "llm_configured": bool(config.llm_api_base and config.llm_model),
                "commands": [*COMMAND_PREFIXES, *sorted(SELF_COMMANDS)],
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("请在群聊中发送“锐评一下 @成员”或“锐评我”。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """解析真实群成员、复用画像并调用租户模型生成一段受控锐评。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    if not config.llm_api_base or not config.llm_model:
        return _error_result("请先配置当前租户自己的模型 API 地址和模型名称。", "llm_not_configured")

    trusted, error = _require_trusted_group_message(db, request.context)
    if error is not None or trusted is None:
        return error or _error_result("当前群聊消息上下文无效。", "trusted_context_required")
    target, target_error = _resolve_target_member(db, trusted)
    if target_error is not None or target is None:
        return target_error or _error_result("没有找到要锐评的群成员。", "target_not_found")

    profile = _load_member_profile(db, trusted, target)
    if profile is None:
        return _error_result(
            "该成员画像尚未生成，请先在插件设置中启用成员画像并等待刷新。",
            "profile_not_ready",
        )
    messages = _load_recent_member_messages(db, trusted, target, config)
    if len(messages) < config.min_message_count:
        return _error_result(
            f"素材不足，最近只有 {len(messages)} 条有效发言，至少需要 {config.min_message_count} 条。",
            "insufficient_messages",
        )

    limiter = PluginRateLimiter(db, trusted.execution)
    user_decision = limiter.consume(
        "user",
        identity=str(trusted.sender.member_external_id),
        limit=config.user_rate_limit,
        window_seconds=config.rate_window_seconds,
    )
    if not user_decision.allowed:
        return _error_result(
            f"锐评请求过于频繁，请在 {user_decision.retry_after_seconds} 秒后重试。",
            "user_rate_limited",
        )
    group_decision = limiter.consume(
        "group",
        identity=str(trusted.conversation.external_id),
        limit=config.group_rate_limit,
        window_seconds=config.rate_window_seconds,
    )
    if not group_decision.allowed:
        return _error_result(
            f"本群锐评请求较多，请在 {group_decision.retry_after_seconds} 秒后重试。",
            "group_rate_limited",
        )

    message_profile = _compress_messages(messages, representative_limit=config.representative_message_limit)
    system_prompt = _build_system_prompt(target.display_name, max_output_chars=config.max_output_chars)
    user_prompt = _build_user_prompt(
        trusted=trusted,
        target=target,
        profile=profile,
        message_profile=message_profile,
    )
    try:
        generated = PluginTextGenerationClient(
            api_base=config.llm_api_base,
            model=config.llm_model,
            api_key=config.llm_api_key,
        ).generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            user_id=(
                f"tenant-{trusted.execution.tenant_id}:binding-{trusted.binding.id}:"
                f"conversation-{trusted.conversation.id}:target-{_short_hash(target.member_id)}"
            ),
            timeout_seconds=config.llm_timeout_seconds,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
        )
    except PluginTextGenerationError:
        return _error_result("锐评模型暂时不可用，请检查当前租户的模型配置。", "llm_failed")

    roast_text = _normalize_roast_text(
        generated.text,
        target_name=target.display_name,
        max_output_chars=config.max_output_chars,
    )
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="群成员锐评已生成。",
        output={
            "provider_status": "completed",
            "target_member_id": target.member_id,
            "target_display_name": target.display_name,
            "sample_message_count": len(messages),
            "token_usage": generated.token_usage,
            "reply_text": roast_text,
            "replies": standard_replies([text_reply(roast_text)]),
        },
    )


def _require_trusted_group_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedRoastMessage | None, PluginExecutionResult | None]:
    """验证租户、绑定、渠道、群会话、入站消息和真实发送成员的完整归属链。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or str(context.chat_type or "") != "group"
        or str(context.message_type or "") != "text"
    ):
        return None, _error_result("请在真实群聊中发送锐评命令。", "trusted_context_required")

    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
    command, _target = _parse_command(str(message.content or "")) if message is not None else ("", "")
    if (
        binding is None
        or int(binding.tenant_id) != int(context.tenant_id)
        or int(binding.bot_id) != int(context.bot_id)
        or binding.status != "enabled"
        or binding.bot is None
        or not binding.bot.is_enabled
        or plugin_code != PLUGIN_CODE
        or channel is None
        or int(channel.tenant_id) != int(context.tenant_id)
        or int(channel.bot_id) != int(context.bot_id)
        or not channel.is_enabled
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or str(conversation.chat_type or "") != "group"
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "") != "in"
        or str(message.message_type or "") != "text"
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
        or not command
    ):
        return None, _error_result("当前锐评消息上下文无效。", "trusted_context_required")

    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.tenant_id == int(context.tenant_id),
            Chatroom.bot_id == int(context.bot_id),
            Chatroom.channel_id == int(channel.id),
            Chatroom.external_id == str(conversation.external_id),
            Chatroom.is_active.is_(True),
        )
    )
    sender = None
    if chatroom is not None:
        sender = db.scalar(
            select(ChatroomMember).where(
                ChatroomMember.tenant_id == int(context.tenant_id),
                ChatroomMember.chatroom_id == int(chatroom.id),
                ChatroomMember.member_external_id == sender_id,
                ChatroomMember.status == "active",
            )
        )
    if chatroom is None or sender is None:
        return None, _error_result("当前无法确认发送人是本群活跃成员。", "group_member_required")
    return TrustedRoastMessage(context, binding, channel, conversation, chatroom, sender, message), None


def _resolve_target_member(
    db: Session,
    trusted: TrustedRoastMessage,
) -> tuple[RoastTarget | None, PluginExecutionResult | None]:
    """只根据落库命令和当前群成员解析目标，不读取执行载荷中的伪造字段。"""

    command, target_token = _parse_command(str(trusted.message.content or ""))
    sender_id = str(trusted.sender.member_external_id or "").strip()
    if command in SELF_COMMANDS or target_token == "我":
        display_name = str(trusted.sender.display_name or trusted.sender.nickname or sender_id).strip() or sender_id
        return RoastTarget(sender_id, display_name), None
    token = str(target_token or "").strip().lstrip("@").strip()
    if not token or len(token) > 128:
        return None, _error_result(
            "请使用“锐评一下 @成员”或“锐评我”，一次只锐评一位成员。",
            "target_required",
        )
    members = db.scalars(
        select(ChatroomMember).where(
            ChatroomMember.chatroom_id == int(trusted.chatroom.id),
            ChatroomMember.status == "active",
            or_(
                ChatroomMember.member_external_id == token,
                ChatroomMember.display_name == token,
                ChatroomMember.nickname == token,
            ),
        )
    ).all()
    unique = {
        str(member.member_external_id or "").strip(): str(
            member.display_name or member.nickname or member.member_external_id
        ).strip()
        for member in members
        if str(member.member_external_id or "").strip()
    }
    if not unique:
        return None, _error_result("当前群没有找到该成员。", "target_not_found")
    if len(unique) > 1:
        return None, _error_result("当前群存在同名成员，请改用唯一账号 ID。", "target_ambiguous")
    member_id, display_name = next(iter(unique.items()))
    return RoastTarget(member_id, display_name or member_id), None


def _load_member_profile(
    db: Session,
    trusted: TrustedRoastMessage,
    target: RoastTarget,
) -> dict[str, Any] | None:
    """读取同一机器人已启用成员画像绑定中的目标画像，不跨机器人或租户查找。"""

    context_binding = db.scalar(
        select(BotPlugin)
        .join(Plugin, Plugin.id == BotPlugin.plugin_id)
        .where(
            BotPlugin.bot_id == int(trusted.binding.bot_id),
            BotPlugin.status == "enabled",
            Plugin.code == PROFILE_PLUGIN_CODE,
        )
        .order_by(BotPlugin.id.asc())
        .limit(1)
    )
    if context_binding is None:
        return None
    state_key = (
        f"channel-{int(trusted.channel.id)}:chatroom-{int(trusted.chatroom.id)}:"
        f"member-{hashlib.sha256(target.member_id.encode('utf-8')).hexdigest()[:24]}"
    )
    record = db.scalar(
        select(PluginStateRecord).where(
            PluginStateRecord.tenant_id == int(trusted.execution.tenant_id),
            PluginStateRecord.bot_plugin_id == int(context_binding.id),
            PluginStateRecord.namespace == PROFILE_NAMESPACE,
            PluginStateRecord.state_key == state_key,
        )
    )
    if record is None:
        return None
    profile = parse_state_payload(record.payload_json)
    if str(profile.get("member_id") or "").strip() != target.member_id:
        return None
    return profile


def _load_recent_member_messages(
    db: Session,
    trusted: TrustedRoastMessage,
    target: RoastTarget,
    config: MemberRoastConfig,
) -> tuple[Message, ...]:
    """读取目标在当前群的有限近期入站文本，明确排除本次锐评命令。"""

    cutoff = now_shanghai() - timedelta(days=config.sample_days)
    rows = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == int(trusted.conversation.id),
            Message.id != int(trusted.message.id),
            Message.direction == "in",
            Message.message_type == "text",
            Message.sender_id == target.member_id,
            func.trim(Message.content) != "",
            Message.created_at >= cutoff,
        )
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(config.message_limit)
    ).all()
    return tuple(reversed(rows))


def _compress_messages(messages: Sequence[Message], *, representative_limit: int) -> dict[str, Any]:
    """把近期消息压缩为稳定模式和少量代表句，避免整段历史进入模型请求。"""

    texts = [_normalize_text(message.content, limit=240) for message in messages]
    texts = [text for text in texts if text]
    token_counter: Counter[str] = Counter()
    sentence_counter: Counter[str] = Counter()
    for text in texts:
        sentence_counter[re.sub(r"\s+", "", text)[:80]] += 1
        for token in TOKEN_PATTERN.findall(text):
            token_counter[token.lower()[:40]] += 1
    repeated = [text for text, count in sentence_counter.most_common(MAX_REPEATED_PHRASES) if count > 1]
    representative = texts[-representative_limit:]
    question_count = sum(1 for text in texts if QUESTION_PATTERN.search(text))
    exclamation_count = sum(1 for text in texts if EXCLAMATION_PATTERN.search(text))
    return {
        "message_count": len(texts),
        "average_length": round(sum(len(text) for text in texts) / max(len(texts), 1), 1),
        "question_ratio": round(question_count / max(len(texts), 1), 3),
        "exclamation_ratio": round(exclamation_count / max(len(texts), 1), 3),
        "frequent_terms": [term for term, _count in token_counter.most_common(8)],
        "repeated_phrases": repeated,
        "representative_messages": representative,
    }


def _build_system_prompt(target_name: str, *, max_output_chars: int) -> str:
    """构建固定安全提示，允许群友式调侃但禁止敏感属性和事实编造。"""

    return (
        "你是群聊中的幽默观察员，请只根据提供的成员画像和消息统计写一段熟人式锐评。"
        "可以犀利、反转和轻度吐槽，但不得辱骂、羞辱外貌，也不得涉及疾病、家庭、职业、"
        "地域、民族、性别、宗教、政治等敏感或未提供的信息。不要写成心理测评或数据报告。"
        f"第一行必须是【锐评 {target_name}】，正文控制在 {max_output_chars} 字以内，最后一句要有收束。"
    )


def _build_user_prompt(
    *,
    trusted: TrustedRoastMessage,
    target: RoastTarget,
    profile: Mapping[str, Any],
    message_profile: Mapping[str, Any],
) -> str:
    """只选取画像白名单和压缩消息统计生成紧凑 JSON 提示词。"""

    meta = profile.get("meta") if isinstance(profile.get("meta"), Mapping) else {}
    payload = {
        "group": {
            "name": _normalize_text(trusted.conversation.title or trusted.chatroom.name or "群聊"),
        },
        "requester": {
            "display_name": _normalize_text(
                trusted.sender.display_name or trusted.sender.nickname or trusted.sender.member_external_id
            ),
        },
        "target": {
            "display_name": target.display_name,
            "activity_level": _normalize_text(profile.get("activity_level")),
            "summary": _normalize_text(profile.get("summary_text"), limit=MAX_PROMPT_TEXT_LENGTH),
            "message_pattern": _normalize_text(profile.get("message_pattern")),
            "interaction_style": _normalize_text(profile.get("interaction_style")),
            "response_style_hint": _normalize_text(profile.get("response_style_hint")),
            "topics": _normalize_list(profile.get("topics_of_interest")),
            "recent_focus": _normalize_list(profile.get("recent_focus")),
            "skill_profile": _normalize_list(meta.get("skill_profile")),
            "problem_solving_profile": _normalize_list(meta.get("problem_solving_profile")),
            "expression_profile": _normalize_list(meta.get("expression_profile")),
            "group_role": _normalize_text(meta.get("group_role")),
        },
        "recent_message_profile": dict(message_profile),
        "instruction": "抓住反复出现的行为和表达方式，像熟人调侃，不得依据单条消息编造长期人设。",
    }
    return json.dumps(_remove_empty(payload), ensure_ascii=False, separators=(",", ":"))


def _normalize_roast_text(value: Any, *, target_name: str, max_output_chars: int) -> str:
    """移除模型代码围栏并强制使用当前目标标题，避免模型替换成员身份。"""

    text = str(value or "").strip()
    text = re.sub(r"^```(?:text|markdown|md)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and re.fullmatch(r"【锐评\s+.+】", lines[0]):
        lines = lines[1:]
    body = "\n".join(lines).strip()
    title = f"【锐评 {target_name}】"
    available = max(20, max_output_chars - len(title) - 1)
    if len(body) > available:
        body = body[:available].rstrip("，,；;、 ") + "…"
    return f"{title}\n{body}" if body else f"{title}\n今天素材有限，先记一笔，等发言再多一点继续观察。"


def _parse_command(content: str) -> tuple[str, str]:
    """解析当前锐评命令及目标文本，不保留旧名字模糊搜索或协议 XML 入口。"""

    normalized = str(content or "").strip()
    if normalized.startswith(("/", "#")):
        normalized = normalized[1:].lstrip()
    compact = re.sub(r"\s+", "", normalized)
    if compact in SELF_COMMANDS:
        return compact, "我"
    for prefix in COMMAND_PREFIXES:
        if normalized == prefix:
            return prefix, ""
        if normalized.startswith(prefix):
            remainder = normalized[len(prefix) :].strip()
            if remainder:
                return prefix, remainder
    return "", ""


def _load_config(raw: Mapping[str, Any]) -> MemberRoastConfig:
    """限制模型温度、采样规模、调用频率和输出长度，控制租户成本与提示词体积。"""

    return MemberRoastConfig(
        llm_api_base=str(raw.get("llm_api_base") or "").strip().rstrip("/"),
        llm_api_key=str(raw.get("llm_api_key") or "").strip(),
        llm_model=str(raw.get("llm_model") or "").strip(),
        llm_timeout_seconds=_bounded_int(raw.get("llm_timeout_seconds"), 60, 10, 180),
        temperature=_bounded_float(raw.get("temperature"), 0.65, 0.0, 1.2),
        max_tokens=_bounded_int(raw.get("max_tokens"), 480, 100, 2000),
        sample_days=_bounded_int(raw.get("sample_days"), 30, 1, 180),
        message_limit=_bounded_int(raw.get("message_limit"), 200, 3, 500),
        min_message_count=_bounded_int(raw.get("min_message_count"), 8, 1, 100),
        representative_message_limit=_bounded_int(raw.get("representative_message_limit"), 8, 1, 20),
        user_rate_limit=_bounded_int(raw.get("user_rate_limit"), 2, 1, 100),
        group_rate_limit=_bounded_int(raw.get("group_rate_limit"), 8, 1, 500),
        rate_window_seconds=_bounded_int(raw.get("rate_window_seconds"), 60, 10, 86400),
        max_output_chars=_bounded_int(raw.get("max_output_chars"), 320, 80, 1000),
    )


def _normalize_list(value: Any) -> list[str]:
    """把画像数组限制为少量短文本，不向模型传递意外嵌套对象。"""

    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _normalize_text(item, limit=80)
        if text and text not in result:
            result.append(text)
        if len(result) >= MAX_PROFILE_LIST_ITEMS:
            break
    return result


def _remove_empty(value: Any) -> Any:
    """递归删除提示词中的空字段，降低无效 token 消耗。"""

    if isinstance(value, Mapping):
        return {
            str(key): normalized
            for key, item in value.items()
            if (normalized := _remove_empty(item)) not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [normalized for item in value if (normalized := _remove_empty(item)) not in (None, "", [], {})]
    return value


def _normalize_text(value: Any, *, limit: int = 160) -> str:
    """压缩空白并限制提示词中的单字段长度。"""

    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _short_hash(value: str) -> str:
    """对外部成员标识生成短哈希，模型服务 user 字段不暴露原账号。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把整数配置限制在业务允许范围。"""

    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    """把浮点配置限制在业务允许范围。"""

    try:
        normalized = float(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    """只接受明确布尔语义。"""

    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """返回面向群用户的有限错误，不暴露模型响应、画像内容或数据库定位。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
