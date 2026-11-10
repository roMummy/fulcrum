from __future__ import annotations

import base64

from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.rate_limit import PluginRateLimiter, PluginStateError
from app.services.plugin_runtime.replies import image_reply, standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore

from plugins.xiuren_image.service import STATE_KEY, STATE_NAMESPACE, load_config, pick_from_pool, refill_pool


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
            output={"commands": list(config.commands), "library_dir": config.library_dir, "prefetch_count": config.prefetch_count},
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("秀人图片只能通过真实消息执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """从预取池随机发一张;池空时先从图片目录随机抽一批(默认 20 张)再发。"""

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
        pool = [str(item) for item in (state.get("pool") or []) if str(item).strip()]
        picked = pick_from_pool(int(context.tenant_id), config, pool)
        if picked is None:
            # 池空或池里的图都不可用:重新从目录抽一批。
            pool = refill_pool(int(context.tenant_id), config)
            picked = pick_from_pool(int(context.tenant_id), config, pool)
        if picked is None:
            store.put(STATE_NAMESPACE, STATE_KEY, {"pool": []})
            return _text_result("图片库里还没有图片，请管理员检查图片目录。", "library_empty")
        data, _chosen, remaining = picked
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
                # 积分不足:这张图留在池里,不浪费。
                store.put(STATE_NAMESPACE, STATE_KEY, {"pool": pool})
                return _error_result(points.message, "insufficient_points")
        # 扣费成功才把这张从池里拿走。
        store.put(STATE_NAMESPACE, STATE_KEY, {"pool": remaining})
        return PluginExecutionResult(
            ok=True,
            status="completed",
            message="已返回一张图片。",
            output={"provider_status": "completed", "replies": standard_replies([image_reply(base64_data=base64.b64encode(data).decode("ascii"))])},
        )
    except (PluginStateError, ValueError):
        return _error_result("图片资源暂时不可用，请稍后重试。", "resource_failed")


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
