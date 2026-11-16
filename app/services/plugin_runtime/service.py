from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import BotPlugin, Chatroom, Conversation, GroupPluginScope
from app.core.logging import get_logger
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.registry import get_plugin_runtime_definition
from app.services.plugin_secrets import decrypt_plugin_secret_config

logger = get_logger(__name__)


def execute_plugin_action(
    plugin_code: str | None,
    action: str,
    *,
    config: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    context: PluginExecutionContext | None = None,
) -> PluginExecutionResult | None:
    definition = get_plugin_runtime_definition(plugin_code)
    if not definition or not definition.executor:
        logger.warning("plugin action skipped because runtime executor is unavailable", extra={"plugin_code": plugin_code or "", "action": action})
        return None
    logger.info("executing plugin action", extra={"plugin_code": plugin_code or "", "action": action})
    return definition.executor.execute(
        PluginExecutionRequest(
            action=action,
            config=dict(config or {}),
            payload=payload or {},
            context=context,
        )
    )


def execute_binding_action(
    binding: BotPlugin,
    action: str,
    *,
    payload: Mapping[str, Any] | None = None,
    context: PluginExecutionContext | None = None,
) -> PluginExecutionResult | None:
    plugin_code, request = _build_binding_request(
        binding,
        action,
        payload=payload,
        context=context,
    )
    return execute_plugin_action(
        plugin_code,
        request.action,
        config=request.config,
        payload=request.payload,
        context=request.context,
    )


def execute_binding_message_action(
    db: Session,
    binding: BotPlugin,
    action: str,
    *,
    payload: Mapping[str, Any] | None = None,
    context: PluginExecutionContext | None = None,
) -> PluginExecutionResult | None:
    """在当前租户会话中执行消息动作，并复用绑定密钥与可信上下文边界。"""

    plugin_code, request = _build_binding_request(
        binding,
        action,
        payload=payload,
        context=context,
        config_override=_group_config_override(db, binding, context),
    )
    definition = get_plugin_runtime_definition(plugin_code)
    executor = definition.executor if definition else None
    message_handler = getattr(executor, "execute_message", None)
    if not callable(message_handler):
        logger.warning(
            "message plugin action skipped because runtime executor is unavailable",
            extra={"plugin_code": plugin_code or "", "action": action},
        )
        return None
    logger.info("executing message plugin action", extra={"plugin_code": plugin_code or "", "action": action})
    return message_handler(db, request)


def execute_binding_scheduled_action(
    db: Session,
    binding: BotPlugin,
    action: str,
    *,
    payload: Mapping[str, Any] | None = None,
    context: PluginExecutionContext | None = None,
) -> PluginExecutionResult | None:
    """在当前租户会话中执行目录插件的显式定时动作入口。"""

    plugin_code, request = _build_binding_request(
        binding,
        action,
        payload=payload,
        context=context,
        config_override=_group_config_override(db, binding, context),
    )
    definition = get_plugin_runtime_definition(plugin_code)
    executor = definition.executor if definition else None
    scheduled_handler = getattr(executor, "execute_scheduled", None)
    if not callable(scheduled_handler):
        logger.warning(
            "scheduled plugin action skipped because runtime executor is unavailable",
            extra={"plugin_code": plugin_code or "", "action": action},
        )
        return None
    logger.info("executing scheduled plugin action", extra={"plugin_code": plugin_code or "", "action": action})
    return scheduled_handler(db, request)


def _build_binding_request(
    binding: BotPlugin,
    action: str,
    *,
    payload: Mapping[str, Any] | None,
    context: PluginExecutionContext | None,
    config_override: Mapping[str, Any] | None = None,
) -> tuple[str | None, PluginExecutionRequest]:
    """从可信绑定统一构造普通、消息和定时请求，确保三条链路使用同一密钥边界。"""

    if binding.status == "unbound":
        raise ValueError("插件已从机器人解绑，请重新绑定后再使用")
    plugin_code = binding.plugin.code if binding.plugin else None
    runtime_payload = dict(payload or {})
    # 群设置只能来自数据库定位的可信会话，绝不从业务 payload 提取。
    from app.services.plugin_schemas import deep_merge_config

    ordinary_config = deep_merge_config(_parse_config_json(binding.config_json), dict(config_override or {}))
    secret_config = decrypt_plugin_secret_config(
        tenant_id=int(binding.tenant_id),
        binding_id=int(binding.id),
        encrypted=binding.secret_config_encrypted,
    )
    # 敏感配置最后合并，确保普通 JSON 中的历史同名字段不能覆盖租户密文。
    effective_config = {**ordinary_config, **secret_config}
    trusted_context = PluginExecutionContext(
        tenant_id=int(binding.tenant_id),
        binding_id=int(binding.id),
        bot_id=int(binding.bot_id),
        # 下列消息定位字段只能由平台内部调用链显式提供。即使业务载荷含有同名键，
        # 也不能自动提升为可信身份，避免手工执行或后续入站接口伪造运行上下文。
        channel_id=_coerce_optional_int(context.channel_id) if context else None,
        conversation_id=_coerce_optional_int(context.conversation_id) if context else None,
        message_id=_coerce_optional_int(context.message_id) if context else None,
        raw_event_id=_coerce_optional_int(context.raw_event_id) if context else None,
        sender_id=_coerce_optional_text(context.sender_id) if context else None,
        channel_type=_coerce_optional_text(context.channel_type) if context else None,
        chat_type=_coerce_optional_text(context.chat_type) if context else None,
        message_type=_coerce_optional_text(context.message_type) if context else None,
    )
    return plugin_code, PluginExecutionRequest(
        action=action,
        config=effective_config,
        payload=runtime_payload,
        context=trusted_context,
    )


def _group_config_override(db: Session, binding: BotPlugin, context: PluginExecutionContext | None) -> dict[str, Any]:
    """原生消息和定时任务共用群配置；完整核对归属后才允许读取覆盖。"""

    from app.services.plugin_directory import get_discovered_plugin

    if not context or not context.conversation_id or not context.channel_id:
        return {}
    if int(context.tenant_id) != int(binding.tenant_id) or int(context.bot_id) != int(binding.bot_id) or int(context.binding_id) != int(binding.id):
        return {}
    conversation = db.get(Conversation, int(context.conversation_id))
    if (conversation is None or conversation.chat_type != 'group'
            or int(conversation.tenant_id) != int(binding.tenant_id)
            or int(conversation.bot_id) != int(binding.bot_id)
            or int(conversation.channel_id or 0) != int(context.channel_id)):
        return {}
    scope = db.scalar(select(GroupPluginScope).join(Chatroom, Chatroom.id == GroupPluginScope.chatroom_id).where(
        GroupPluginScope.bot_plugin_id == binding.id, GroupPluginScope.channel_id == context.channel_id,
        GroupPluginScope.tenant_id == binding.tenant_id, Chatroom.tenant_id == binding.tenant_id,
        Chatroom.bot_id == binding.bot_id, Chatroom.channel_id == context.channel_id, Chatroom.external_id == conversation.external_id,
    ))
    config = _parse_config_json(scope.config_override_json) if scope else {}
    code = binding.plugin.code if binding.plugin else None
    definition = get_plugin_runtime_definition(code)
    discovered = get_discovered_plugin(code)
    sensitive = set(discovered.secret_fields) if discovered else set()
    # 群层不持有密钥；历史覆盖里的敏感同名字段也不能绕过绑定密文边界。
    if definition and definition.schema:
        sensitive.update(field.name for field in definition.schema.fields if field.sensitive)
    for name in sensitive:
        config.pop(name, None)
    return config


def _parse_config_json(raw_config: str | None) -> dict[str, Any]:
    normalized = (raw_config or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_optional_int(value: Any) -> int | None:
    """把服务端构造的可选主键转换为正整数，非法值不进入可信上下文。"""

    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _coerce_optional_text(value: Any) -> str | None:
    """规范化可信执行载荷中的可选文本定位字段。"""

    normalized = str(value or "").strip()
    return normalized or None
