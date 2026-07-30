from __future__ import annotations

from app.core.db import SessionLocal
from app.services.customer_operations import submit_customer_operation
from app.services.plugin_runtime.contracts import PluginExecutionRequest, PluginExecutionResult


def execute_action(request: PluginExecutionRequest) -> PluginExecutionResult:
    if request.action == "reload":
        return PluginExecutionResult(ok=True, status="running", message="customer_operations runtime is ready", output={})
    if request.action != "execute":
        return PluginExecutionResult(ok=False, status="unsupported", message=f"不支持的动作: {request.action}")

    with SessionLocal() as db:
        try:
            result = submit_customer_operation(db, request.payload)
        except ValueError as exc:
            return PluginExecutionResult(ok=False, status="error", message=str(exc), output={})
        db.commit()

    return PluginExecutionResult(
        ok=bool(result.get("ok")),
        status=str(result.get("status") or "completed"),
        message=str(result.get("status_reason") or result.get("status") or "客户运营请求已处理"),
        output=result,
    )
