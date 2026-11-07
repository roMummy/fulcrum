from __future__ import annotations

import json
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.models.entities import BotPlugin
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
) -> tuple[str | None, PluginExecutionRequest]:
    """从可信绑定统一构造普通、消息和定时请求，确保三条链路使用同一密钥边界。"""

    plugin_code = binding.plugin.code if binding.plugin else None
    runtime_payload = dict(payload or {})
    ordinary_config = _parse_config_json(binding.config_json)
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
