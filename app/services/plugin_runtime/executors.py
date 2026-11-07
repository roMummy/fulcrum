from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.plugin_directory import get_plugin_default_config, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


class DirectoryRuntimeExecutor:
    def __init__(self, plugin_code: str):
        self.plugin_code = plugin_code

    def execute(self, request: PluginExecutionRequest) -> PluginExecutionResult:
        loaded = load_directory_plugin(self.plugin_code, force=request.action == "reload")
        if not loaded or not loaded.instance or not hasattr(loaded.instance, "execute_action"):
            return PluginExecutionResult(
                ok=False,
                status="error",
                message=f"{self.plugin_code} 未提供目录运行时 execute_action 入口。",
            )
        merged_request = PluginExecutionRequest(
            action=request.action,
            config={**get_plugin_default_config(self.plugin_code, force=request.action == "reload"), **dict(request.config)},
            payload=request.payload,
            context=request.context,
        )
        result = loaded.instance.execute_action(merged_request)
        return self._normalize_result(result)

    def execute_message(self, db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
        """执行消息类动作，并只向显式声明数据库感知入口的插件传递会话。"""

        loaded = load_directory_plugin(self.plugin_code, force=request.action == "reload")
        if not loaded or not loaded.instance:
            return PluginExecutionResult(
                ok=False,
                status="error",
                message=f"{self.plugin_code} 目录运行时不可用。",
            )
        merged_request = PluginExecutionRequest(
            action=request.action,
            config={
                **get_plugin_default_config(self.plugin_code, force=request.action == "reload"),
                **dict(request.config),
            },
            payload=request.payload,
            context=request.context,
        )
        message_handler = getattr(loaded.instance, "execute_message_action", None)
        if callable(message_handler):
            # 数据库会话只进入显式消息入口。普通目录插件仍调用 execute_action，
            # 因此无法因为平台新增通用能力而获得任意数据库访问权限。
            return self._normalize_result(message_handler(db, merged_request))
        action_handler = getattr(loaded.instance, "execute_action", None)
        if callable(action_handler):
            return self._normalize_result(action_handler(merged_request))
        return PluginExecutionResult(
            ok=False,
            status="error",
            message=f"{self.plugin_code} 未提供可执行的消息动作入口。",
        )

    def execute_scheduled(self, db: Session, request: PluginExecutionRequest) -> PluginExecutionResult:
        """调用目录插件显式声明的定时动作入口。"""

        loaded = load_directory_plugin(self.plugin_code)
        if not loaded or not loaded.instance or not hasattr(loaded.instance, "execute_scheduled_action"):
            return PluginExecutionResult(
                ok=False,
                status="unsupported",
                message=f"{self.plugin_code} 未提供 execute_scheduled_action 定时入口。",
            )
        merged_request = PluginExecutionRequest(
            action=request.action,
            config={**get_plugin_default_config(self.plugin_code), **dict(request.config)},
            payload=request.payload,
            context=request.context,
        )
        result = loaded.instance.execute_scheduled_action(db, merged_request)
        return self._normalize_result(result)

    def _normalize_result(self, value: Any) -> PluginExecutionResult:
        if isinstance(value, PluginExecutionResult):
            return value
        if isinstance(value, dict):
            output = value.get("output")
            return PluginExecutionResult(
                ok=bool(value.get("ok", True)),
                status=str(value.get("status") or ("completed" if value.get("ok", True) else "error")),
                message=str(value.get("message") or ""),
                output=output if isinstance(output, dict) else {},
            )
        return PluginExecutionResult(ok=True, status="completed", message=str(value or ""), output={})
