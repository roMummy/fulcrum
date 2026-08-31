from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import (
    BotPlugin,
    Channel,
    Chatroom,
    Conversation,
    GroupPluginScope,
    Message,
    TaskJob,
)
from app.services.plugin_runtime.contracts import (
    PluginExecutionContext,
    PluginExecutionRequest,
    PluginExecutionResult,
)
from app.services.plugin_runtime.maibot_client import (
    MaiBotConnectionConfig,
    MaiBotConnectionKey,
    maibot_client_registry,
    stop_maibot_connection,
)
from app.services.plugin_runtime.replies import PluginReplyError, text_reply
from app.services.task_queue import enqueue_job
from app.services.workwechat_cloud_poll import notify_cloud_job_enqueued, resolve_outbound_job_type


PLUGIN_CODE = "maibot_adapter"
MAX_MESSAGE_TEXT_LENGTH = 8_000
MAX_REPLY_SEGMENT_DEPTH = 3
SUPPORTED_MESSAGE_TYPES = {
    "text",
    "image",
    "file",
    "video",
    "voice",
    "location",
    "app",
    "emoji",
    "system",
}
MESSAGE_PLACEHOLDERS = {
    "image": "[图片]",
    "file": "[文件]",
    "video": "[视频]",
    "voice": "[语音]",
    "location": "[位置]",
    "app": "[应用消息]",
    "emoji": "[表情]",
    "system": "[系统消息]",
}


@dataclass(frozen=True)
class MaiBotPluginConfig:
    """保存当前租户绑定的采集、回复和连接配置。"""

    enabled: bool
    collect_group_messages: bool
    collect_private_messages: bool
    enable_reply_output: bool
    reply_group_messages: bool
    reply_private_messages: bool
    api_server_ws_url: str
    api_key: str
    platform_name: str
    verify_ssl: bool
    connect_timeout_seconds: int
    receive_timeout_seconds: int
    heartbeat_interval_seconds: int
    reconnect_delay_seconds: int
    queue_maxsize: int
    max_send_retries: int

    def connection_config(self) -> MaiBotConnectionConfig:
        """只把传输字段交给当前绑定自己的 WebSocket 连接。"""

        return MaiBotConnectionConfig(
            ws_url=self.api_server_ws_url,
            api_key=self.api_key,
            platform_name=self.platform_name,
            verify_ssl=self.verify_ssl,
            connect_timeout_seconds=self.connect_timeout_seconds,
            receive_timeout_seconds=self.receive_timeout_seconds,
            heartbeat_interval_seconds=self.heartbeat_interval_seconds,
            reconnect_delay_seconds=self.reconnect_delay_seconds,
            queue_maxsize=self.queue_maxsize,
            max_send_retries=self.max_send_retries,
        )


@dataclass(frozen=True)
class TrustedMaiBotMessage:
    """保存经过数据库交叉验证的插件绑定和入站消息归属链。"""

    execution: PluginExecutionContext
    binding: BotPlugin
    channel: Channel
    conversation: Conversation
    message: Message


def matches_event(context) -> dict[str, object]:
    """按聊天类型和租户采集开关匹配旁路消息，不占用命令入口。"""

    chat_type = str(getattr(context, "chat_type", "") or "").strip().lower()
    message_type = str(getattr(context, "message_type", "") or "").strip().lower()
    config = _load_config(getattr(context, "binding_config", {}) or {})
    collection_enabled = (
        config.collect_group_messages if chat_type == "group" else config.collect_private_messages
    )
    matched = (
        config.enabled
        and chat_type in {"direct", "group"}
        and message_type in SUPPORTED_MESSAGE_TYPES
        and collection_enabled
    )
    return {
        "matched": matched,
        "reason": "maibot_collection_enabled" if matched else "maibot_collection_disabled",
        "debug": {"chat_type": chat_type, "message_type": message_type},
    }


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    """重载时停止旧连接，下一条真实消息再按当前配置建立连接。"""

    config = _load_config(request.config)
    context = request.context
    if request.action == "reload":
        if context is not None:
            stop_maibot_connection(context.tenant_id, context.binding_id)
        if not config.enabled:
            return PluginExecutionResult(
                ok=True,
                status="disabled",
                message="MaiBot 适配器当前未启用。",
                output={"enabled": False, "provider_configured": False},
            )
        configured = bool(_is_valid_ws_url(config.api_server_ws_url) and config.api_key)
        return PluginExecutionResult(
            ok=True,
            status="running" if configured else "waiting_config",
            message="MaiBot 配置已加载，连接将在下一条消息时建立。" if configured else "请配置 MaiBot 地址和 API Key。",
            output={
                "enabled": True,
                "provider_configured": configured,
                "collect_group_messages": config.collect_group_messages,
                "collect_private_messages": config.collect_private_messages,
                "enable_reply_output": config.enable_reply_output,
                "reply_group_messages": config.reply_group_messages,
                "reply_private_messages": config.reply_private_messages,
            },
        )
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")
    return _ignored_result("trusted_context_required", "MaiBot 采集必须来自真实入站消息。")


def execute_message_action(db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
    """把可信入站消息提交给当前租户绑定自己的 MaiBot 连接。"""

    if request.action != "execute":
        return execute_action(request)
    config = _load_config(request.config)
    if not config.enabled:
        if request.context is not None:
            stop_maibot_connection(request.context.tenant_id, request.context.binding_id)
        return _ignored_result("disabled", "MaiBot 适配器当前未启用。")

    trusted, context_error = _require_trusted_message(db, request.context)
    if context_error is not None or trusted is None:
        return context_error or _ignored_result("trusted_context_required", "当前消息上下文无效。")
    chat_type = str(trusted.conversation.chat_type or "direct").strip()
    if chat_type == "group" and not config.collect_group_messages:
        return _ignored_result("collection_disabled", "当前绑定未采集群消息。")
    if chat_type == "direct" and not config.collect_private_messages:
        return _ignored_result("collection_disabled", "当前绑定未采集私聊消息。")
    if not _is_valid_ws_url(config.api_server_ws_url) or not config.api_key:
        return _ignored_result("not_configured", "请先配置当前租户自己的 MaiBot 地址和 API Key。")

    message_text = _normalize_message_text(trusted.message)
    if not message_text:
        return _ignored_result("empty_message", "当前消息没有可采集内容。")
    api_message = _build_api_message(
        trusted,
        config=config,
        message_text=message_text,
        mentioned_self=bool(request.payload.get("mentioned_self")),
    )
    queued = maibot_client_registry.submit(
        key=MaiBotConnectionKey(
            tenant_id=int(trusted.execution.tenant_id),
            binding_id=int(trusted.binding.id),
        ),
        config=config.connection_config(),
        api_message=api_message,
        conversation_id=int(trusted.conversation.id),
        reply_handler=_handle_maibot_reply,
    )
    if not queued:
        return _ignored_result("queue_full", "MaiBot 发送队列繁忙，本次消息未采集。")
    return PluginExecutionResult(
        ok=True,
        status="completed",
        message="消息已提交给 MaiBot。",
        output={"provider_status": "queued", "replies": []},
    )


def _require_trusted_message(
    db: Session,
    context: PluginExecutionContext | None,
) -> tuple[TrustedMaiBotMessage | None, PluginExecutionResult | None]:
    """交叉验证租户、绑定、机器人、渠道、会话和入站消息的完整归属链。"""

    if (
        context is None
        or context.channel_id is None
        or context.conversation_id is None
        or context.message_id is None
        or context.chat_type not in {"direct", "group"}
        or context.message_type not in SUPPORTED_MESSAGE_TYPES
    ):
        return None, _ignored_result("trusted_context_required", "请从真实入站消息启动 MaiBot 采集。")

    binding = db.get(BotPlugin, int(context.binding_id))
    channel = db.get(Channel, int(context.channel_id))
    conversation = db.get(Conversation, int(context.conversation_id))
    message = db.get(Message, int(context.message_id))
    plugin_code = str(binding.plugin.code or "") if binding is not None and binding.plugin is not None else ""
    sender_id = str(message.sender_id or "").strip() if message is not None else ""
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
        or str(channel.channel_type or "").strip() != str(context.channel_type or "").strip()
        or conversation is None
        or int(conversation.tenant_id) != int(context.tenant_id)
        or int(conversation.bot_id) != int(context.bot_id)
        or int(conversation.channel_id or 0) != int(channel.id)
        or str(conversation.chat_type or "").strip() != str(context.chat_type or "").strip()
        or not str(conversation.external_id or "").strip()
        or message is None
        or int(message.tenant_id) != int(context.tenant_id)
        or int(message.conversation_id) != int(conversation.id)
        or str(message.direction or "").strip() != "in"
        or str(message.message_type or "").strip() != str(context.message_type or "").strip()
        or not sender_id
        or (context.sender_id is not None and str(context.sender_id).strip() != sender_id)
    ):
        return None, _ignored_result("trusted_context_required", "当前消息上下文无效。")

    return (
        TrustedMaiBotMessage(
            execution=context,
            binding=binding,
            channel=channel,
            conversation=conversation,
            message=message,
        ),
        None,
    )


def _build_api_message(
    trusted: TrustedMaiBotMessage,
    *,
    config: MaiBotPluginConfig,
    message_text: str,
    mentioned_self: bool,
) -> dict[str, Any]:
    """构造当前 MaiBot APIMessageBase，使用内部会话令牌隔离出站目标。"""

    conversation_token = f"mbs-conversation-{int(trusted.conversation.id)}"
    sender_id = str(trusted.message.sender_id or "").strip()
    sender_name = _single_line(trusted.message.sender_name or sender_id, limit=128) or sender_id
    chat_type = str(trusted.conversation.chat_type or "direct").strip()
    if chat_type == "group":
        user_id = f"mbs-user-{int(trusted.channel.id)}-{sender_id}"
        group_info: dict[str, str] | None = {
            "platform": config.platform_name,
            "group_id": conversation_token,
            "group_name": _single_line(
                trusted.conversation.title or trusted.conversation.external_id,
                limit=255,
            ),
        }
    else:
        # 私聊回复也必须落回当前平台会话，因此 user_id 使用内部会话令牌。
        # 原始发送人只作为显示名，不作为 MaiBot 可任意指定的出站目标。
        user_id = conversation_token
        group_info = None
    user_info = {
        "platform": config.platform_name,
        "user_id": user_id,
        "user_nickname": sender_name,
    }
    message_id = str(trusted.message.external_message_id or "").strip() or f"mbs-message-{int(trusted.message.id)}"
    created_at = trusted.message.created_at
    timestamp = float(created_at.timestamp()) if created_at is not None else 0.0
    return {
        "message_info": {
            "platform": config.platform_name,
            "message_id": message_id,
            "time": timestamp,
            "format_info": {
                "content_format": ["text"],
                "accept_format": ["text"],
            },
            "additional_config": {
                "source": "multi_bot_saas_maibot_adapter",
                "is_at": mentioned_self,
                "at_bot": mentioned_self,
                "is_mentioned": mentioned_self,
                "message_type": str(trusted.message.message_type or "text"),
                "collect_only": not config.enable_reply_output,
            },
            "sender_info": {
                "user_info": user_info,
                "group_info": group_info,
            },
        },
        "message_segment": {"type": "text", "data": message_text},
        "message_dim": {
            "api_key": config.api_key,
            "platform": config.platform_name,
        },
    }


def _handle_maibot_reply(
    key: MaiBotConnectionKey,
    conversation_id: int,
    package: Mapping[str, Any],
) -> None:
    """重新加载当前租户资源，把客户端验证后的会话回复写入标准出站任务。"""

    with tenant_scope(int(key.tenant_id)), tenant_session(TenantSessionLocal) as db:
        binding = db.get(BotPlugin, int(key.binding_id))
        conversation = db.get(Conversation, int(conversation_id))
        if (
            binding is None
            or int(binding.tenant_id) != int(key.tenant_id)
            or binding.status != "enabled"
            or binding.plugin is None
            or str(binding.plugin.code or "") != PLUGIN_CODE
            or binding.bot is None
            or not binding.bot.is_enabled
            or conversation is None
            or int(conversation.tenant_id) != int(key.tenant_id)
            or int(conversation.bot_id) != int(binding.bot_id)
        ):
            return
        channel = db.get(Channel, int(conversation.channel_id or 0))
        if (
            channel is None
            or int(channel.tenant_id) != int(key.tenant_id)
            or int(channel.bot_id) != int(binding.bot_id)
            or not channel.is_enabled
        ):
            return

        config = _load_config(_parse_json_object(binding.config_json))
        chat_type = str(conversation.chat_type or "direct").strip()
        if not config.enabled or not config.enable_reply_output:
            return
        if chat_type == "group" and (not config.reply_group_messages or not _group_reply_allowed(db, binding, channel, conversation)):
            return
        if chat_type == "direct" and not config.reply_private_messages:
            return

        reply_text = _extract_reply_text(package)
        if not reply_text:
            return
        try:
            normalized_reply = text_reply(reply_text)
        except PluginReplyError:
            return

        package_id = str(package.get("msg_id") or "").strip()
        dedupe_material = package_id or json.dumps(package, ensure_ascii=False, sort_keys=True)
        dedupe_key = f"maibot:{int(binding.id)}:{hashlib.sha256(dedupe_material.encode('utf-8')).hexdigest()}"
        existing = db.scalar(select(TaskJob).where(TaskJob.dedupe_key == dedupe_key))
        if existing is not None:
            return
        outbound_payload = {
            "conversation_id": int(conversation.id),
            "external_id": str(conversation.external_id),
            "receiver_id": str(conversation.external_id),
            "chat_type": chat_type,
            "message_type": "text",
            "content": normalized_reply["content"],
        }
        job = enqueue_job(
            db,
            job_type=resolve_outbound_job_type(channel),
            payload={
                "channel_id": int(channel.id),
                "conversation_id": int(conversation.id),
                "payload": outbound_payload,
                "plugin_code": PLUGIN_CODE,
            },
            priority=90,
            related_type="conversation",
            related_id=int(conversation.id),
            dedupe_key=dedupe_key,
            max_attempts=2,
        )
        notify_cloud_job_enqueued(channel, job)
        db.commit()


def _group_reply_allowed(
    db: Session,
    binding: BotPlugin,
    channel: Channel,
    conversation: Conversation,
) -> bool:
    """异步回包再次检查当前群级强制关闭状态。"""

    chatroom = db.scalar(
        select(Chatroom).where(
            Chatroom.channel_id == int(channel.id),
            Chatroom.external_id == str(conversation.external_id),
        )
    )
    if chatroom is None:
        return True
    scope = db.scalar(
        select(GroupPluginScope).where(
            GroupPluginScope.channel_id == int(channel.id),
            GroupPluginScope.chatroom_id == int(chatroom.id),
            GroupPluginScope.bot_plugin_id == int(binding.id),
        )
    )
    return scope is None or bool(scope.enabled)


def _extract_reply_text(package: Mapping[str, Any]) -> str:
    payload = package.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    segment = payload.get("message_segment")
    text = _extract_segment_text(segment, depth=0)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()
    return text[:20_000]


def _extract_segment_text(segment: Any, *, depth: int) -> str:
    """只解析官方文本与文本片段列表，忽略外部媒体和任意对象。"""

    if depth > MAX_REPLY_SEGMENT_DEPTH or not isinstance(segment, Mapping):
        return ""
    segment_type = str(segment.get("type") or "").strip().lower()
    data = segment.get("data")
    if segment_type == "text":
        return str(data or "")
    if segment_type == "seglist" and isinstance(data, list):
        return "".join(_extract_segment_text(item, depth=depth + 1) for item in data[:20])
    return ""


def _normalize_message_text(message: Message) -> str:
    message_type = str(message.message_type or "text").strip().lower()
    content = re.sub(r"\s+", " ", str(message.content or "")).strip()
    if message_type == "text":
        return content[:MAX_MESSAGE_TEXT_LENGTH]
    placeholder = MESSAGE_PLACEHOLDERS.get(message_type, f"[{message_type or '消息'}]")
    if content and content != placeholder:
        return f"{placeholder} {content}"[:MAX_MESSAGE_TEXT_LENGTH]
    return placeholder


def _load_config(raw_config: Mapping[str, Any]) -> MaiBotPluginConfig:
    raw = dict(raw_config)
    platform_name = _single_line(raw.get("platform_name") or "multi-bot-saas", limit=64) or "multi-bot-saas"
    return MaiBotPluginConfig(
        enabled=_as_bool(raw.get("enabled"), default=False),
        collect_group_messages=_as_bool(raw.get("collect_group_messages"), default=True),
        collect_private_messages=_as_bool(raw.get("collect_private_messages"), default=True),
        enable_reply_output=_as_bool(raw.get("enable_reply_output"), default=False),
        reply_group_messages=_as_bool(raw.get("reply_group_messages"), default=True),
        reply_private_messages=_as_bool(raw.get("reply_private_messages"), default=True),
        api_server_ws_url=str(raw.get("api_server_ws_url") or "").strip(),
        api_key=str(raw.get("api_key") or "").strip(),
        platform_name=platform_name,
        verify_ssl=_as_bool(raw.get("verify_ssl"), default=True),
        connect_timeout_seconds=_bounded_int(raw.get("connect_timeout_seconds"), default=15, minimum=5, maximum=60),
        receive_timeout_seconds=_bounded_int(raw.get("receive_timeout_seconds"), default=120, minimum=30, maximum=600),
        heartbeat_interval_seconds=_bounded_int(raw.get("heartbeat_interval_seconds"), default=20, minimum=10, maximum=120),
        reconnect_delay_seconds=_bounded_int(raw.get("reconnect_delay_seconds"), default=5, minimum=1, maximum=60),
        queue_maxsize=_bounded_int(raw.get("queue_maxsize"), default=200, minimum=20, maximum=2_000),
        max_send_retries=_bounded_int(raw.get("max_send_retries"), default=2, minimum=1, maximum=5),
    )


def _is_valid_ws_url(value: str) -> bool:
    parsed = urlsplit(str(value or "").strip())
    return (
        parsed.scheme.lower() in {"ws", "wss"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _parse_json_object(raw_json: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw_json or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _single_line(value: Any, *, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized[:limit]


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


def _ignored_result(provider_status: str, message: str) -> PluginExecutionResult:
    """观察者插件不向聊天发送前置错误，只在执行状态中记录原因。"""

    return PluginExecutionResult(
        ok=False,
        status="ignored",
        message=message,
        output={"provider_status": provider_status, "replies": []},
    )
