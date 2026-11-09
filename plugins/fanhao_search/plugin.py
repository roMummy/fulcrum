from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.core.tenant_context import require_tenant_id
from app.models.entities import MessageAttachment
from app.services.points import PointsService
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.plugin_runtime.rate_limit import PluginRateLimiter, PluginStateError
from app.services.plugin_runtime.replies import standard_replies, text_reply

from plugins.fanhao_search.service import format_image_result, format_movie_result, fetch_best_magnet, fetch_movie_detail, load_config, normalize_code, search_image


def matches_event(context) -> dict[str, object]:
    """匹配文本番号命令和以图查番命令，具体附件归属在消息执行阶段校验。"""

    content = str(getattr(context, "content", "") or "").strip()
    config = load_config(getattr(context, "binding_config", {}) or {})
    command = content.split(maxsplit=1)[0].lstrip("/#") if content else ""
    matched = command in config.commands or (config.image_search_enabled and command in config.image_search_commands)
    return {
        "matched": matched,
        "reason": "fanhao_command" if matched else "fanhao_command_required",
        "debug": {"command": command},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供重载信息，查询必须使用可信消息入口。"""

    config = load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(
            ok=True,
            status="running",
            message="番号查询插件运行正常。",
            output={
                "commands": list(config.commands),
                "image_search_enabled": config.image_search_enabled,
                "javbus_site_count": len(config.javbus_urls),
                "allow_download_link": config.allow_download_link,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("番号查询只能通过真实消息执行。", "trusted_context_required")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """执行文本查询或引用图片查番，并在成功后扣除当前租户积分。"""

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

        command_name = str(request.payload.get("command_name") or "").strip()
        command_args = str(request.payload.get("command_args") or "").strip()
        client = PluginHttpClient()
        if config.image_search_enabled and command_name in config.image_search_commands:
            return _handle_image_search(db, client, config, request, context)

        code = normalize_code(command_args)
        if not code:
            return _text_result(f"命令格式：{config.commands[0]} 番号，例如：{config.commands[0]} SSIS-406", "invalid_parameters")
        detail = fetch_movie_detail(client, config, code)
        if detail is None:
            return _text_result("未找到番号，或当前站点暂时无法访问。", "not_found")
        magnet = fetch_best_magnet(client, config, detail) if config.allow_download_link else ""
        result = _text_result(format_movie_result(detail, magnet), "completed")
        points_error = _deduct_points(db, context, config, "text", code)
        if points_error is not None:
            return points_error
        return result
    except (PluginStateError, ValueError):
        return _error_result("番号查询暂时失败，请稍后再试。", "query_failed")


def _handle_image_search(db: Session, client: PluginHttpClient, config: FanhaoConfig, request: PluginExecutionRequest, context: PluginExecutionContext) -> PluginExecutionResult:
    """只读取平台已下载且属于当前消息链路的图片附件。"""

    image_bytes = _load_reference_image(db, context, request.payload)
    if not image_bytes:
        return _text_result("请先引用一张已接收的图片，再发送查番。", "image_required")
    try:
        matches = search_image(client, config, image_bytes)
    except ValueError as exc:
        message = str(exc)
        if "太频繁" in message:
            return _error_result(message, "image_rate_limited")
        return _error_result("以图查番暂时失败，请换一张清晰图片后再试。", "image_search_failed")
    if not matches:
        return _text_result("未找到相似度足够高的番号，可以裁掉水印或边框后再试。", "image_empty")
    detail = None
    if config.avscan_fetch_detail:
        detail = fetch_movie_detail(client, config, matches[0].code)
    points_error = _deduct_points(db, context, config, "image", matches[0].code)
    if points_error is not None:
        return points_error
    return _text_result(format_image_result(matches, detail), "completed")


def _load_reference_image(db: Session, context: PluginExecutionContext, payload: Mapping[str, Any]) -> bytes:
    """按当前消息或可信引用消息编号查询已下载图片，不接受载荷直接给出的路径。"""

    candidate_message_ids = [int(context.message_id or 0)]
    normalized_event = payload.get("normalized_event")
    if isinstance(normalized_event, Mapping):
        for key in ("quoted_message_id", "reply_to_message_id", "refer_message_id"):
            try:
                value = int(normalized_event.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                candidate_message_ids.append(value)
        details = normalized_event.get("details")
        if isinstance(details, Mapping):
            for key in ("quoted_message_id", "reply_to_message_id", "refer_message_id"):
                try:
                    value = int(details.get(key) or 0)
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    candidate_message_ids.append(value)
    data_root = Path(settings.data_dir).expanduser().resolve(strict=True)
    for message_id in dict.fromkeys(candidate_message_ids):
        if message_id <= 0:
            continue
        attachment = db.scalar(
            select(MessageAttachment)
            .where(
                MessageAttachment.message_id == message_id,
                MessageAttachment.channel_id == int(context.channel_id or 0),
                MessageAttachment.conversation_id == int(context.conversation_id or 0),
                MessageAttachment.direction == "in",
                MessageAttachment.attachment_type == "image",
                MessageAttachment.status == "downloaded",
                MessageAttachment.tenant_id == int(context.tenant_id),
            )
            .order_by(MessageAttachment.id.asc())
            .limit(1)
        )
        if attachment is None or not attachment.storage_path:
            continue
        try:
            image_path = Path(str(attachment.storage_path)).expanduser().resolve(strict=True)
            image_path.relative_to(data_root)
            if image_path.stat().st_size <= config_max_image_bytes(payload):
                return image_path.read_bytes()
        except (OSError, ValueError):
            continue
    return b""


def config_max_image_bytes(payload: Mapping[str, Any]) -> int:
    """附件读取只使用固定平台上限，不能由消息载荷放大。"""

    return 8 * 1024 * 1024


def _deduct_points(db: Session, context: PluginExecutionContext, config: FanhaoConfig, mode: str, key: str) -> PluginExecutionResult | None:
    if config.points_cost <= 0:
        return None
    result = PointsService(db).spend(
        context,
        config.points_cost,
        source="fanhao_search",
        description="番号查询消费",
        metadata={"binding_id": int(context.binding_id), "mode": mode},
        idempotency_key=f"fanhao:{int(context.binding_id)}:{int(context.message_id or 0)}:{mode}:{key}",
    )
    return _error_result(result.message, "insufficient_points") if not result.ok else None


def _validate_context(context: PluginExecutionContext | None) -> PluginExecutionResult | None:
    if context is None:
        return _error_result("番号查询缺少可信消息上下文。", "trusted_context_required")
    if str(context.chat_type or "").strip().lower() not in {"direct", "group"}:
        return _error_result("当前会话类型不支持番号查询。", "chat_type_unsupported")
    if not str(context.sender_id or "").strip() or int(context.message_id or 0) <= 0:
        return _error_result("当前消息缺少可信发送人或消息编号。", "message_context_invalid")
    return None



def _text_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(ok=True, status=status, message=message, output={"provider_status": status, "replies": standard_replies([text_reply(message)])})


def _error_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(ok=False, status=status, message=message, output={"provider_status": status, "replies": standard_replies([text_reply(message)])})

