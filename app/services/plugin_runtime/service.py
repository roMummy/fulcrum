from __future__ import annotations

import json
from typing import Any, Mapping

from app.models.entities import BotPlugin
from app.core.logging import get_logger
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult
from app.services.plugin_runtime.registry import get_plugin_runtime_definition

logger = get_logger(__name__)


def execute_plugin_action(
    plugin_code: str | None,
    action: str,
    *,
    config_json: str | None,
    payload: Mapping[str, Any] | None = None,
) -> PluginExecutionResult | None:
    definition = get_plugin_runtime_definition(plugin_code)
    if not definition or not definition.executor:
        logger.warning("plugin action skipped because runtime executor is unavailable", extra={"plugin_code": plugin_code or "", "action": action})
        return None
    logger.info("executing plugin action", extra={"plugin_code": plugin_code or "", "action": action})
    return definition.executor.execute(
        PluginExecutionRequest(
            action=action,
            config=_parse_config_json(config_json),
            payload=payload or {},
        )
    )


def execute_binding_action(
    binding: BotPlugin,
    action: str,
    *,
    payload: Mapping[str, Any] | None = None,
) -> PluginExecutionResult | None:
    plugin_code = binding.plugin.code if binding.plugin else None
    return execute_plugin_action(plugin_code, action, config_json=binding.config_json, payload=payload)


def execute_configured_plugin_action(
    plugin_code: str | None,
    action: str,
    *,
    config: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> PluginExecutionResult | None:
    definition = get_plugin_runtime_definition(plugin_code)
    if not definition or not definition.executor:
        logger.warning("configured plugin action skipped because runtime executor is unavailable", extra={"plugin_code": plugin_code or "", "action": action})
        return None
    logger.info("executing configured plugin action", extra={"plugin_code": plugin_code or "", "action": action})
    return definition.executor.execute(
        PluginExecutionRequest(
            action=action,
            config=dict(config or {}),
            payload=payload or {},
        )
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
