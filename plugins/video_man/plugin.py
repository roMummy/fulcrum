from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.plugin_runtime.rate_limit import PluginRateLimiter, PluginStateError
from app.services.plugin_runtime.replies import standard_replies, text_reply, video_reply

from .service import load_config, fetch_video


def matches_event(context) -> dict[str, object]:
    """只匹配当前租户绑定已经启用的猛男视频命令。"""

    content = str(getattr(context, "content", "") or "").strip()
    config = load_config(getattr(context, "binding_config", {}) or {})
    command = content.split(maxsplit=1)[0].lstrip("/#") if content else ""
    matched = bool(config.enabled and command in config.commands)
    return {
        "matched": matched,
        "reason": "video_man_command" if matched else "video_man_command_required",
        "debug": {"command": command, "commands": list(config.commands)},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供重载信息，下载和扣费必须使用可信消息入口。"""

    config = load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="猛男视频插件运行正常。",
            output={
                "enabled": config.enabled,
                "commands": list(config.commands),
                "api_url_configured": bool(config.api_url),
                "max_video_seconds": config.max_video_seconds,
                "points_cost": config.points_cost,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("猛男视频只能通过真实消息执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """在当前租户可信消息上下文中限流、扣费并返回标准视频回复。"""

    if request.action != "execute":
        return execute_action(request)
    config = load_config(request.config)
    if not config.enabled:
        return _error_result("猛男视频当前未启用。", "disabled")
    context = request.context
    error = _validate_context(context)
    if error is not None:
        return error
    assert context is not None
    try:
        if int(context.tenant_id) != require_tenant_id():
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
        if config.points_cost > 0:
            points = PointsService(db).deduct_points(
                str(context.sender_id),
                _group_id(context),
                config.points_cost,
                source="video_man",
                description="猛男视频插件消费",
                metadata={"binding_id": int(context.binding_id), "message_id": int(context.message_id or 0)},
                idempotency_key=f"video-man:{int(context.binding_id)}:{int(context.message_id or 0)}",
            )
            if not points.ok:
                return _error_result(points.message, "insufficient_points")
        result = fetch_video(PluginHttpClient(), int(context.tenant_id), config)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="视频已准备完成。",
            output={
                "provider_status": "completed",
                "replies": standard_replies(
                    [
                        video_reply(
                            video_base64=result.video_base64,
                            cover_base64=result.cover_base64,
                            play_length=result.duration_seconds,
                        )
                    ]
                ),
            },
        )
    except (PluginStateError, ValueError):
        return _error_result("视频暂时不可用，请稍后重试。", "video_failed")


def _validate_context(context: PluginExecutionContext | None) -> PluginExecutionResult | None:
    if context is None:
        return _error_result("猛男视频缺少可信消息上下文。", "trusted_context_required")
    if str(context.chat_type or "").strip().lower() not in {"direct", "group"}:
        return _error_result("当前会话类型不支持视频回复。", "chat_type_unsupported")
    if not str(context.sender_id or "").strip() or int(context.message_id or 0) <= 0:
        return _error_result("当前消息缺少可信发送人或消息编号。", "message_context_invalid")
    return None


def _group_id(context: PluginExecutionContext) -> str:
    prefix = "group" if str(context.chat_type or "").strip().lower() == "group" else "direct"
    return f"{prefix}:{int(context.conversation_id or context.bot_id)}"


def _error_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(
        ok=False,
        status=status,
        message=message,
        output={"provider_status": status, "replies": standard_replies([text_reply(message)])},
    )

