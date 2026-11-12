"""同步适配器调用 Agent 命令队列的请求-响应桥。

适配器运行在后台 worker/请求线程里,不能直接等事件循环回调,所以这里用
独立租户会话入队命令,再用短查询轮询任务结果。相比进程内 Future,
数据库轮询在多 worker 部署下也能拿到一致的命令结果。
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.core.db import TenantSessionLocal
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, TaskJob

from .contract import JOB_TYPE
from .service import enqueue_command


def call_command(
    channel: Channel,
    command_name: str,
    params: dict[str, Any],
    *,
    timeout: float = 15.0,
    purpose: str = "",
    priority: int = 10,
) -> dict[str, Any]:
    """下发读类命令并等待手机回报;超时或失败抛 RuntimeError。"""

    tenant_id = int(channel.tenant_id)
    job_id = 0
    with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
        fresh = db.get(Channel, int(channel.id))
        if fresh is None:
            raise RuntimeError("咕嘎助手渠道不存在")
        job = enqueue_command(
            db,
            fresh,
            command_name=command_name,
            params=params,
            purpose=purpose,
            priority=priority,
        )
        db.commit()
        job_id = int(job.id)

    deadline = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < deadline:
        with tenant_scope(tenant_id), tenant_session(TenantSessionLocal) as db:
            job = db.get(TaskJob, job_id)
            if job is not None and job.job_type == JOB_TYPE and job.status in {"completed", "failed"}:
                if job.status == "failed":
                    raise RuntimeError(job.error_message or f"{command_name}_failed")
                try:
                    result = json.loads(job.result_json or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    result = {}
                return result if isinstance(result, dict) else {}
        time.sleep(0.15)
    raise RuntimeError(f"{command_name}_timeout")
