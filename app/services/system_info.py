"""平台后台的服务器配置与数据库使用情况采集。

所有采集都是只读、轻量查询;数据库部分按 MySQL/MariaDB 的 information_schema 读取,
表条目为存储引擎估算值(InnoDB 与精确值有偏差),只用于了解量级。
"""

from __future__ import annotations

import os
import platform
import time

import psutil
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import engine


def _gb(value: float) -> float:
    return round(value / 1024**3, 2)


def build_server_info() -> dict:
    """服务器(容器可见范围)的 CPU、内存、磁盘、负载与当前进程占用。"""

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    process = psutil.Process(os.getpid())
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "cpu_cores": psutil.cpu_count(logical=True),
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "load": f"{load1:.2f} / {load5:.2f} / {load15:.2f}",
        "memory_total_gb": _gb(memory.total),
        "memory_used_gb": _gb(memory.total - memory.available),
        "memory_percent": memory.percent,
        "disk_total_gb": _gb(disk.total),
        "disk_used_gb": _gb(disk.used),
        "disk_percent": disk.percent,
        "boot_seconds": int(time.time() - psutil.boot_time()),
        "process_rss_mb": round(process.memory_info().rss / 1024**2, 1),
        "process_threads": process.num_threads(),
        "process_uptime_seconds": int(time.time() - process.create_time()),
    }


def build_database_info(db: Session, top_tables: int = 40) -> dict:
    """数据库版本、容量、连接、响应延迟与各表条目数(按数据体积倒序)。"""

    info: dict = {"dialect": engine.dialect.name, "tables": [], "error": ""}
    started = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        info["ping_ms"] = round((time.perf_counter() - started) * 1000, 1)
    except Exception as exc:  # 数据库异常时页面仍要能打开并展示原因
        info["error"] = str(exc)[:200]
        return info

    pool = engine.pool
    info["pool"] = {
        "size": getattr(pool, "size", lambda: 0)(),
        "checked_out": getattr(pool, "checkedout", lambda: 0)(),
        "overflow": getattr(pool, "overflow", lambda: 0)(),
    }
    if engine.dialect.name not in {"mysql", "mariadb"}:
        return info

    try:
        info["version"] = db.execute(text("SELECT VERSION()")).scalar()
        rows = db.execute(
            text(
                "SELECT table_name, table_rows, data_length, index_length FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
            )
        ).all()
        tables = [
            {
                "name": row[0],
                "rows": int(row[1] or 0),
                "data_mb": round(int(row[2] or 0) / 1024**2, 2),
                "index_mb": round(int(row[3] or 0) / 1024**2, 2),
            }
            for row in rows
        ]
        tables.sort(key=lambda item: item["data_mb"] + item["index_mb"], reverse=True)
        info["table_count"] = len(tables)
        info["total_rows"] = sum(item["rows"] for item in tables)
        info["data_mb"] = round(sum(item["data_mb"] for item in tables), 1)
        info["index_mb"] = round(sum(item["index_mb"] for item in tables), 1)
        info["tables"] = tables[:top_tables]
        status = dict(
            db.execute(
                text(
                    "SHOW GLOBAL STATUS WHERE Variable_name IN "
                    "('Threads_connected','Threads_running','Uptime','Slow_queries','Questions','Max_used_connections')"
                )
            ).all()
        )
        info["status"] = {key: int(value) for key, value in status.items()}
    except Exception as exc:
        info["error"] = str(exc)[:200]
    return info
