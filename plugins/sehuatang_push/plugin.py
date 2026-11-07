from __future__ import annotations

from urllib.parse import quote

from sqlalchemy.orm import Session

from app.core.settings import settings
from app.core.time import now_shanghai
from app.models.entities import Channel, Conversation
from app.services.outbound_batch import enqueue_outbound_job
from app.services.outbound_files import store_outbound_upload
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.http_client import PluginHttpClient
from app.services.plugin_runtime.replies import standard_replies, text_reply
from app.services.plugin_runtime.state_store import PluginStateStore

from plugins.sehuatang_push.service import load_config, build_pdf


STATE_NAMESPACE = "sehuatang_push"
STATE_KEY = "delivery"


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """普通入口只提供重载信息，PDF 抓取必须由统一调度动作触发。"""

    config = load_config(request.config)
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="涩图 PDF 推送插件运行正常。", output={"enabled": config.enabled, "target_count": len(config.target_conversation_ids), "source_count": len(config.source_urls)})
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _error_result("涩图 PDF 推送只能通过统一计划任务执行。", "scheduled_only")


def execute_scheduled_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """生成租户 PDF、保存文件并为当前目标会话创建幂等文件出站任务。"""

    if request.action != "daily_pdf_push":
        return _error_result(f"不支持的涩图 PDF 动作: {request.action}", "unsupported")
    config = load_config(request.config)
    if not config.enabled:
        return _error_result("涩图 PDF 推送当前未启用。", "disabled")
    context = request.context
    if context is None or not context.conversation_id:
        return _error_result("PDF 推送缺少目标会话上下文。", "trusted_context_required")
    conversation = db.get(Conversation, int(context.conversation_id))
    if conversation is None or int(conversation.tenant_id) != int(context.tenant_id) or int(conversation.bot_id) != int(context.bot_id):
        return _error_result("PDF 推送目标会话不属于当前租户机器人。", "conversation_mismatch")
    channel = db.get(Channel, int(conversation.channel_id or 0))
    if channel is None:
        return _error_result("PDF 推送渠道不存在。", "channel_missing")
    if not str(settings.public_base_url or "").strip():
        return _error_result("平台尚未配置公共文件访问地址。", "public_base_url_required")
    try:
        output_path, movie_count, digest = build_pdf(PluginHttpClient(), int(context.tenant_id), config)
        content = output_path.read_bytes()
        stored = store_outbound_upload(
            tenant_id=int(context.tenant_id),
            channel_id=int(channel.id),
            file_name=f"sehuatang-{now_shanghai().strftime('%Y%m%d')}.pdf",
            content=content,
            content_type="application/pdf",
            message_type="file",
        )
        download_url = f"{str(settings.public_base_url).rstrip('/')}/outbound-files/{int(channel.id)}/{stored['share_id']}/{quote(stored['file_name'])}"
        job = enqueue_outbound_job(
            db,
            conversation=conversation,
            channel=channel,
            payload={"message_type": "file", "file_name": stored["file_name"], "file_url": download_url},
            plugin_code="sehuatang_push",
            related_type="plugin_sehuatang_push",
            related_id=int(context.binding_id),
            priority=70,
            dedupe_key=f"sehuatang:{int(context.binding_id)}:{now_shanghai().strftime('%Y-%m-%d')}:{int(conversation.id)}:{digest}",
            max_attempts=1,
        )
        PluginStateStore(db, context).put(
            STATE_NAMESPACE,
            STATE_KEY,
            {"date": now_shanghai().strftime("%Y-%m-%d"), "conversation_id": int(conversation.id), "digest": digest, "movie_count": movie_count, "job_id": int(job.id)},
            ttl_seconds=config.retention_seconds,
        )
        return PluginExecutionResult(ok=True, status="completed", message=f"PDF 已创建并入队，包含 {movie_count} 部影片。", output={"provider_status": "completed", "movie_count": movie_count, "outbound_job_id": int(job.id)})
    except Exception:
        return _error_result("PDF 生成或推送失败，请稍后重试。", "push_failed")


def _error_result(message: str, status: str) -> PluginExecutionResult:
    return PluginExecutionResult(ok=False, status=status, message=message, output={"provider_status": status, "replies": standard_replies([text_reply(message)])})

