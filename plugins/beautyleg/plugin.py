from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.rate_limit import PluginRateLimiter, PluginStateError
from app.services.plugin_runtime.replies import standard_replies, text_reply

from plugins.beautyleg.service import build_image_reply, load_config, select_random_image


def matches_event(context) -> dict[str, object]:
    """只匹配配置命令，渠道和会话范围由平台 manifest 先行过滤。"""

    content = str(getattr(context, "content", "") or "").strip()
    config = load_config(getattr(context, "binding_config", {}) or {})
    command = content.split(maxsplit=1)[0].lstrip("/#") if content else ""
    matched = bool(config.enabled and command in config.commands)
    return {
        "matched": matched,
        "reason": "beautyleg_command" if matched else "beautyleg_command_required",
        "debug": {"command": command, "commands": list(config.commands)},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供重载和健康信息，业务发送必须来自可信消息入口。"""

    config = load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="美腿图片插件运行正常。",
            output={
                "enabled": config.enabled,
                "commands": list(config.commands),
                "resource_dir": config.resource_dir,
                "points_cost": config.points_cost,
                "rate_limit": config.rate_limit,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("美腿图片只能通过真实消息执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在当前租户可信消息上下文中扣除积分并返回一张随机图片。"""

    if request.action != "execute":
        return execute_action(request)
    config = load_config(request.config)
    if not config.enabled:
        return _error_result("美腿图片当前未启用。", "disabled")
    context = request.context
    error = _validate_context(context)
    if error is not None:
        return error
    assert context is not None
    try:
        tenant_id = require_tenant_id()
        if int(context.tenant_id) != tenant_id:
            return _error_result("当前插件上下文不属于当前租户。", "tenant_mismatch")
        scope = "group" if str(context.chat_type or "").strip().lower() == "group" else "user"
        identity = context.conversation_id if scope == "group" else context.sender_id
        decision = PluginRateLimiter(db, context).consume(
            scope,
            identity=identity,
            limit=config.rate_limit,
            window_seconds=config.rate_window_seconds,
        )
        if not decision.allowed:
            return _error_result(f"请求太频繁，请 {decision.retry_after_seconds} 秒后再试。", "rate_limited")

        sender_id = str(context.sender_id or "").strip()
        group_id = _group_id(context)
        points = PointsService(db).deduct_points(
            sender_id,
            group_id,
            config.points_cost,
            source="beautyleg",
            description="美腿图片消费",
            metadata={"binding_id": int(context.binding_id), "message_id": int(context.message_id or 0)},
            idempotency_key=f"beautyleg:{int(context.binding_id)}:{int(context.message_id or 0)}",
        ) if config.points_cost > 0 else None
        if points is not None and not points.ok:
            return _error_result(points.message, "insufficient_points")

        image_path = select_random_image(int(context.tenant_id), config)
        if image_path is None:
            return _text_result("当前还没有可用的图片资源。", "resource_empty")
        reply = build_image_reply(image_path, config)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="已返回一张图片。",
            output={"provider_status": "completed", "replies": standard_replies([reply])},
        )
    except (PluginStateError, ValueError):
        return _error_result("图片资源暂时不可用，请稍后重试。", "resource_failed")


def _validate_context(context: PluginExecutionContext | None) -> PluginExecutionResult | None:
    """校验消息入口所需的最小可信定位信息。"""

    if context is None:
        return _error_result("美腿图片缺少可信消息上下文。", "trusted_context_required")
    if str(context.chat_type or "").strip().lower() not in {"direct", "group"}:
        return _error_result("当前会话类型不支持图片回复。", "chat_type_unsupported")
    if not str(context.sender_id or "").strip() or int(context.message_id or 0) <= 0:
        return _error_result("当前消息缺少可信发送人或消息编号。", "message_context_invalid")
    return None


def _group_id(context: PluginExecutionContext) -> str:
    """把平台会话主键转换为积分账本的稳定分组标识。"""

    prefix = "group" if str(context.chat_type or "").strip().lower() == "group" else "direct"
    return f"{prefix}:{int(context.conversation_id or context.bot_id)}"


def _text_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=True,
        status=status,
        message=message,
        output={"provider_status": status, "replies": standard_replies([text_reply(message)])},
    )


def _error_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status=status,
        message=message,
        output={"provider_status": status, "replies": standard_replies([text_reply(message)])},
    )

