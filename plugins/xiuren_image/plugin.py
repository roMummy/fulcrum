from __future__ import annotations

import base64
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.core.tenant_context import require_tenant_id
from app.models.entities import BotPlugin
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.plugin_runtime.rate_limit import PluginRateLimiter, PluginStateError
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply
from app.services.plugin_runtime.scheduler import enqueue_plugin_scheduled_action
from app.services.plugin_runtime.state_store import PluginStateStore

from plugins.xiuren_image.service import STATE_KEY, STATE_NAMESPACE, choose_cached_image, load_config, refresh_cached_images


def matches_event(context) -> dict[str, object]:
    """只匹配已启用绑定的随机图片命令。"""

    content = str(getattr(context, "content", "") or "").strip()
    config = load_config(getattr(context, "binding_config", {}) or {})
    command = content.split(maxsplit=1)[0].lstrip("/#") if content else ""
    matched = command in config.commands
    return {"matched": matched, "reason": "xiuren_image_command" if matched else "xiuren_image_command_required", "debug": {"command": command}}


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供重载信息，图片读取必须使用可信消息入口。"""

    config = load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="秀人图片插件运行正常。",
            output={"commands": list(config.commands), "cache_size": config.cache_size, "refresh_enabled": config.refresh_enabled},
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("秀人图片只能通过真实消息执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """从当前租户绑定状态读取图片，缓存为空时只创建一次受控刷新任务。"""

    if request.action != "execute":
        return execute_action(request)
    config = load_config(request.config)
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
        decision = PluginRateLimiter(db, context).consume(scope, identity=identity, limit=config.rate_limit, window_seconds=config.rate_window_seconds)
        if not decision.allowed:
            return _error_result(f"请求太频繁，请 {decision.retry_after_seconds} 秒后再试。", "rate_limited")
        store = PluginStateStore(db, context)
        state = store.get(STATE_NAMESPACE, STATE_KEY) or {}
        selected = choose_cached_image(int(context.tenant_id), config, state)
        if selected is None:
            _enqueue_refresh_if_needed(db, context, config)
            return _text_result("图片资源正在准备中，请稍后再试。", "refresh_queued")
        if config.points_cost > 0:
            points = PointsService(db).spend(
                context,
                config.points_cost,
                source="xiuren_image",
                description="秀人图片插件消费",
                metadata={"binding_id": int(context.binding_id), "message_id": int(context.message_id or 0)},
                idempotency_key=f"xiuren-image:{int(context.binding_id)}:{int(context.message_id or 0)}",
            )
            if not points.ok:
                return _error_result(points.message, "insufficient_points")
        data, _item = selected
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="已返回一张图片。",
            output={"provider_status": "completed", "replies": standard_replies([image_reply(base64_data=base64.b64encode(data).decode("ascii"))])},
        )
    except (PluginStateError, ValueError):
        return _error_result("图片资源暂时不可用，请稍后重试。", "resource_failed")


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """执行租户绑定级缓存刷新，状态写入与文件写入保持同一绑定边界。"""

    if request.action != "refresh_resources":
        return _error_result(f"不支持的秀人图片动作: {request.action}", "unsupported")
    context = request.context
    if context is None:
        return _error_result("缓存刷新缺少可信插件上下文。", "trusted_context_required")
    config = load_config(request.config)
    if not config.refresh_enabled:
        return _error_result("秀人图片刷新未启用。", "disabled")
    try:
        store = PluginStateStore(db, context)
        previous = store.get(STATE_NAMESPACE, STATE_KEY) or {}
        state = refresh_cached_images(PluginHttpClient(), int(context.tenant_id), config, previous)
        store.put(STATE_NAMESPACE, STATE_KEY, state)
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message=f"图片缓存刷新完成，新增 {int(state.get('downloaded') or 0)} 张。",
            output={"provider_status": "completed", "downloaded": int(state.get("downloaded") or 0), "failed": int(state.get("failed") or 0), "cached_count": len(state.get("files") or [])},
        )
    except (PluginStateError, ValueError):
        return _error_result("图片缓存刷新失败，请稍后重试。", "refresh_failed")


def _enqueue_refresh_if_needed(db: Session, context: PluginExecutionContext, config) -> None:
    """缓存为空时按当前绑定和当前时间幂等入队，不在消息线程执行网络抓取。"""

    if not config.refresh_enabled or not config.source_urls:
        return
    binding = db.get(BotPlugin, int(context.binding_id))
    if binding is None:
        return
    enqueue_plugin_scheduled_action(
        db,
        binding,
        action_key="refresh_resources",
        scheduled_for=now_shanghai() + timedelta(seconds=1),
        parameters={},
        high_side_effect=False,
        priority=70,
    )


def _validate_context(context: PluginExecutionContext | None) -> PluginExecutionResult | None:
    if context is None:
        return _error_result("秀人图片缺少可信消息上下文。", "trusted_context_required")
    if str(context.chat_type or "").strip().lower() not in {"direct", "group"}:
        return _error_result("当前会话类型不支持图片回复。", "chat_type_unsupported")
    if not str(context.sender_id or "").strip() or int(context.message_id or 0) <= 0:
        return _error_result("当前消息缺少可信发送人或消息编号。", "message_context_invalid")
    return None



def _text_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(ok=True, status=status, message=message, output={"provider_status": status, "replies": standard_replies([text_reply(message)])})


def _error_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(ok=False, status=status, message=message, output={"provider_status": status, "replies": standard_replies([text_reply(message)])})

