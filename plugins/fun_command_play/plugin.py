from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.entities import BotPlugin, Channel, Conversation, Message
from app.models.plugin_business import FunCommandRule
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.rate_limit import PluginRateLimiter
from app.services.plugin_runtime.replies import (
    PluginReplyError,
    image_reply,
    link_reply,
    standard_replies,
    text_reply,
    video_reply,
)


PLUGIN_CODE = "fun_command_play"
MAX_ACTIVE_RULES = 1_000
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 30 * 1024 * 1024


@dataclass(frozen=True)
class FunCommandConfig:
    """保存当前机器人绑定唯一生效的趣味命令运行参数。"""

    allow_direct_messages: bool
    max_replies_per_rule: int


@dataclass(frozen=True)
class TrustedFunCommandMessage:
    """保存经过数据库归属校验的绑定、渠道、会话和入站消息。"""

    context: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message


def matches_event(context) -> dict[str, object]:
    """观察当前绑定允许的标准文本，具体规则匹配必须进入可信数据库入口。"""

    config = _load_config(getattr(context, "binding_config", {}) or {})
    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "").strip().lower()
    content = str(getattr(context, "content", "") or "").strip()
    matched = (
        message_type == "text"
        and bool(content)
        and chat_type in {"direct", "group"}
        and (chat_type != "direct" or config.allow_direct_messages)
    )
    return {
        "matched": matched,
        "reason": "fun_command_candidate" if matched else "standard_text_required",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只报告运行状态，规则执行必须来自平台保存的真实入站消息。"""

    config = _load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="趣味命令插件已就绪。",
            output={
                "allow_direct_messages": config.allow_direct_messages,
                "max_replies_per_rule": config.max_replies_per_rule,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("趣味命令只能由真实聊天消息触发。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """查询当前机器人启用规则，执行首条命中且未处于冷却期的标准回复。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)

    trusted, error = _require_message_context(db, request.context)
    if error is not None or trusted is None:
        return error or _error_result("趣味命令缺少可信消息上下文。", "trusted_context_required")
    if trusted.conversation.chat_type == "direct" and not config.allow_direct_messages:
        return _ignored("当前绑定未开放私聊趣味命令。", "direct_disabled")

    content = str(trusted.message.content or "").strip()
    rules = db.scalars(
        select(FunCommandRule)
        .where(
            FunCommandRule.tenant_id == int(trusted.context.tenant_id),
            FunCommandRule.bot_plugin_id == int(trusted.binding.id),
            FunCommandRule.is_enabled.is_(True),
        )
        .order_by(FunCommandRule.priority.asc(), FunCommandRule.id.asc())
        .limit(MAX_ACTIVE_RULES)
    ).all()
    cooldown_blocked = False
    for rule in rules:
        if not _rule_matches(rule, content):
            continue
        if not _consume_rule_cooldown(db, trusted, rule):
            cooldown_blocked = True
            continue
        try:
            replies = _build_rule_replies(trusted, rule, config.max_replies_per_rule)
        except (PluginReplyError, ValueError, OSError):
            return _error_result("当前趣味命令规则配置不可用，请联系管理员处理。", "rule_invalid")
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=f"已执行趣味命令“{rule.name}”。",
            output={
                "provider_status": "matched",
                "rule_id": int(rule.id),
                "rule_name": str(rule.name),
                "reply_count": len(replies),
                "replies": replies,
            },
        )

    if cooldown_blocked:
        return _ignored("命中的趣味命令仍在冷却中。", "cooldown")
    return _ignored("当前消息未命中趣味命令。", "rule_not_matched")


def _require_message_context(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedFunCommandMessage | None, PluginExecutionResult | None]:
    """验证所有消息定位字段都来自同一租户和机器人，业务载荷不能覆盖这些主键。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
    ):
        return None, _error_result("趣味命令缺少真实消息上下文。", "trusted_context_required")
    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
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
        or str(conversation.chat_type or "").strip().lower() not in {"direct", "group"}
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip().lower() != "in"
        or str(message.message_type or "").strip().lower() != "text"
        or not str(message.content or "").strip()
    ):
        return None, _error_result("当前趣味命令消息上下文无效。", "trusted_context_required")
    sender_id = str(message.sender_id or "").strip()
    if context.sender_id is not None and str(context.sender_id).strip() != sender_id:
        return None, _error_result("当前趣味命令发送人上下文无效。", "trusted_context_required")
    return TrustedFunCommandMessage(context, binding, channel, conversation, message), None


def _rule_matches(rule: FunCommandRule, content: str) -> bool:
    """按当前规则唯一匹配方式判断文本，不执行用户可编辑正则表达式。"""

    trigger = str(rule.trigger_text or "").strip()
    if not trigger:
        return False
    match_type = str(rule.match_type or "exact").strip().lower()
    if match_type == "exact":
        return content == trigger
    if match_type == "prefix":
        return content.startswith(trigger)
    if match_type == "contains":
        return trigger in content
    return False


def _consume_rule_cooldown(
    db: Session,
    trusted: TrustedFunCommandMessage,
    rule: FunCommandRule,
) -> bool:
    """按群会话或私聊用户建立跨进程冷却窗口，零秒规则不产生状态记录。"""

    cooldown_seconds = max(0, min(int(rule.cooldown_seconds or 0), 86_400))
    if cooldown_seconds <= 0:
        return True
    limiter = PluginRateLimiter(db, trusted.context)
    if trusted.conversation.chat_type == "group":
        decision = limiter.consume(
            "group",
            identity=f"rule:{int(rule.id)}:conversation:{int(trusted.conversation.id)}",
            limit=1,
            window_seconds=cooldown_seconds,
        )
    else:
        sender_id = str(trusted.message.sender_id or "anonymous").strip() or "anonymous"
        decision = limiter.consume(
            "user",
            identity=f"rule:{int(rule.id)}:sender:{sender_id}",
            limit=1,
            window_seconds=cooldown_seconds,
        )
    return decision.allowed


def _build_rule_replies(
    trusted: TrustedFunCommandMessage,
    rule: FunCommandRule,
    maximum: int,
) -> list[dict[str, Any]]:
    """把规则动作转换为平台标准回复，并在读取前固定租户媒体根目录。"""

    try:
        actions = json.loads(str(rule.replies_json or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("规则回复不是合法 JSON") from exc
    if not isinstance(actions, list) or not actions:
        raise ValueError("规则回复必须是非空数组")
    media_root = _tenant_media_root(int(trusted.context.tenant_id))
    template_values = _template_values(trusted)
    replies: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, Mapping):
            raise ValueError("规则动作必须是对象")
        action_type = str(action.get("type") or "").strip().lower()
        if action_type == "text":
            content = _render_template(str(action.get("content") or ""), template_values)
            replies.append(text_reply(content))
        elif action_type == "image":
            image_path = _resolve_tenant_media_path(
                media_root,
                _render_template(str(action.get("path") or ""), template_values),
            )
            _assert_file_size(image_path, MAX_IMAGE_BYTES, "图片")
            replies.append(image_reply(image_path=image_path, allowed_root=media_root))
        elif action_type == "video":
            video_path = _resolve_tenant_media_path(
                media_root,
                _render_template(str(action.get("path") or ""), template_values),
            )
            cover_path = _resolve_tenant_media_path(
                media_root,
                _render_template(str(action.get("cover_path") or ""), template_values),
            )
            video_base64 = _read_media_base64(video_path, MAX_VIDEO_BYTES, "视频")
            cover_base64 = _read_media_base64(cover_path, MAX_IMAGE_BYTES, "视频封面")
            replies.append(
                video_reply(
                    video_base64=video_base64,
                    cover_base64=cover_base64,
                    play_length=_optional_non_negative_int(action.get("play_length")),
                )
            )
        elif action_type == "link":
            replies.append(
                link_reply(
                    title=_render_template(str(action.get("title") or ""), template_values),
                    description=_render_template(str(action.get("description") or ""), template_values),
                    url=_render_template(str(action.get("url") or ""), template_values),
                    thumb_url=_render_template(str(action.get("thumb_url") or ""), template_values),
                )
            )
        else:
            raise ValueError("规则动作类型不受支持")
    return standard_replies(replies, max_count=maximum, allowed_root=media_root)


def _tenant_media_root(tenant_id: int) -> Path:
    """返回当前租户唯一媒体目录，不自动回退到共享或历史插件目录。"""

    return (Path(settings.data_dir) / "tenants" / str(int(tenant_id)) / "fun_command").resolve()


def _resolve_tenant_media_path(media_root: Path, relative_path: str) -> Path:
    """只接受租户目录内的相对文件名，绝对路径和目录穿越直接拒绝。"""

    normalized = str(relative_path or "").strip()
    candidate_path = Path(normalized)
    if not normalized or candidate_path.is_absolute():
        raise ValueError("媒体文件必须使用租户目录内的相对路径")
    root = media_root.resolve(strict=True)
    candidate = (root / candidate_path).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("媒体文件不属于当前租户目录") from exc
    if not candidate.is_file():
        raise ValueError("媒体文件不存在")
    return candidate


def _assert_file_size(path: Path, maximum_bytes: int, label: str) -> None:
    """在读取媒体前限制原始文件体积，避免编码后超过平台回复上限。"""

    file_size = path.stat().st_size
    if file_size <= 0 or file_size > maximum_bytes:
        raise ValueError(f"{label}文件为空或超过平台限制")


def _read_media_base64(path: Path, maximum_bytes: int, label: str) -> str:
    """读取已验证的租户媒体并转换为标准回复需要的 base64。"""

    _assert_file_size(path, maximum_bytes, label)
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _template_values(trusted: TrustedFunCommandMessage) -> dict[str, str]:
    """只暴露稳定业务字段给轻量模板，不提供任意对象或表达式执行能力。"""

    return {
        "sender": str(trusted.message.sender_id or ""),
        "sender_name": str(trusted.message.sender_name or trusted.message.sender_id or ""),
        "conversation": str(trusted.conversation.title or trusted.conversation.external_id or ""),
        "content": str(trusted.message.content or ""),
    }


def _render_template(template: str, values: Mapping[str, str]) -> str:
    """执行固定占位符替换，未知占位符按原文保留。"""

    rendered = str(template or "")
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value or ""))
    return rendered


def _optional_non_negative_int(value: Any) -> int | None:
    """解析可选视频时长，空值不进入回复载荷。"""

    if value in (None, ""):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("视频时长不合法") from exc
    if normalized < 0 or normalized > 86_400:
        raise ValueError("视频时长不合法")
    return normalized


def _load_config(raw_config: Mapping[str, Any]) -> FunCommandConfig:
    """读取当前绑定配置并把回复数量限制在平台标准上限内。"""

    raw = dict(raw_config)
    return FunCommandConfig(
        allow_direct_messages=_as_bool(raw.get("allow_direct_messages"), default=True),
        max_replies_per_rule=_bounded_int(
            raw.get("max_replies_per_rule"),
            default=8,
            minimum=1,
            maximum=8,
        ),
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _ignored(message: str, provider_status: str) -> PluginExecutionResult:
    """返回无出站回复的正常跳过结果。"""

    return PluginExecutionResult(
        ok=True,
        status="skipped",
        message=message,
        output={"provider_status": provider_status, "replies": []},
    )


def _error_result(message: str, provider_status: str) -> PluginExecutionResult:
    """返回业务可读错误，不暴露规则 JSON、媒体路径或数据库异常。"""

    return PluginExecutionResult(
        ok=False,
        status="error",
        message=message,
        output={
            "provider_status": provider_status,
            "replies": standard_replies([text_reply(message)]),
        },
    )
