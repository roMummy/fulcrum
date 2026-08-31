from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.plugin_directory import get_plugin_default_config, load_directory_plugin
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


class PlaceholderExecutor:
    def __init__(self, plugin_code: str):
        self.plugin_code = plugin_code

    def execute(self, request: PluginExecutionRequest) -> PluginExecutionResult:
        if request.action == "reload":
            return PluginExecutionResult(
                ok=True,
                status="running",
                message=f"{self.plugin_code} 已接入当前项目插件目录，等待继续补齐执行逻辑。",
                output={
                    "plugin_code": self.plugin_code,
                    "migration_state": "scaffolded",
                    "config_keys": sorted(request.config.keys()),
                },
            )

        if request.action != "execute":
            return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

        return PluginExecutionResult(
            ok=True,
            status="stub",
            message=f"{self.plugin_code} 已完成迁移骨架接入，当前执行链路已接管，但具体业务逻辑仍待继续补齐。",
            output={
                "plugin_code": self.plugin_code,
                "migration_state": "scaffolded",
                "content": str(request.payload.get("content") or ""),
                "command_name": request.payload.get("command_name"),
                "conversation_id": request.payload.get("conversation_id"),
            },
        )


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
