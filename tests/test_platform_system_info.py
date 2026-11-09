"""平台服务器/数据库信息与响应耗时统计的轻量测试。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="system-info-logs-"))

from app.core.db import SessionLocal
from app.services import request_metrics
from app.services.system_info import build_database_info, build_server_info


def test_request_metrics_aggregates_by_route():
    request_metrics._samples.clear()
    for ms in (10, 20, 30, 1000):
        request_metrics.record_request("GET /x", 200, ms)
    request_metrics.record_request("GET /y", 500, 5)
    result = request_metrics.summarize()
    assert result["overall"]["count"] == 5
    assert result["overall"]["errors"] == 1
    assert result["slowest"][0]["route"] == "GET /x"
    assert result["slowest"][0]["max_ms"] == 1000


def test_server_and_database_info_shape():
    server = build_server_info()
    assert server["cpu_cores"] and 0 <= server["memory_percent"] <= 100
    with SessionLocal() as db:
        info = build_database_info(db)
    assert "dialect" in info
