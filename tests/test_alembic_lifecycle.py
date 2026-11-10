from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DELETED_TABLES = {"bot_groups", "config_templates", "bot_tags", "bot_tag_links"}
DELETED_BOT_COLUMNS = {"slug", "command_prefix", "group_id", "config_template_id", "environment"}
PLATFORM_ANALYTICS_INDEXES = {
    "messages": "ix_messages_platform_analytics",
    "message_activity_hourly": "ix_message_activity_hourly_platform_analytics",
    "tenant_sessions": "ix_tenant_sessions_platform_presence",
    "channels": "ix_channels_platform_health",
    "billing_orders": "ix_billing_orders_platform_revenue",
    "tenant_subscriptions": "ix_tenant_subscriptions_platform_health",
    "bot_bindings": "ix_bot_bindings_platform_usage",
    "provider_nodes": "ix_provider_nodes_platform_presence",
}


def _run_alembic(database_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """在独立 SQLite 数据库中运行迁移，确保测试不会连接开发或生产实例。"""

    runtime_dir = database_path.parent / "runtime"
    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATA_DIR": str(runtime_dir / "data"),
            "DATABASE_URL": f"sqlite:///{database_path}",
            "LOG_DIR": str(runtime_dir / "logs"),
            "AUTH_SEED_DEMO_USERS": "false",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _current_revision(database_path: Path) -> str | None:
    """直接读取版本表，避免测试结果依赖 Alembic 命令行输出格式。"""

    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    return str(row[0]) if row else None


def test_alembic_upgrades_empty_database_to_simplified_baseline(tmp_path: Path) -> None:
    """空库升级后必须精确匹配精简模型，且不能恢复机器人旧业务结构。"""

    database_path = tmp_path / "saas.db"
    _run_alembic(database_path, "upgrade", "head")

    from app.core.db import Base
    from app.models import entities  # noqa: F401
    from app.models import saas  # noqa: F401
    from app.services.database_lifecycle import expected_database_revision

    migration_engine = create_engine(f"sqlite:///{database_path}")
    inspector = inspect(migration_engine)
    actual_tables = set(inspector.get_table_names()) - {"alembic_version"}

    assert _current_revision(database_path) == expected_database_revision()
    assert actual_tables == set(Base.metadata.tables)
    assert DELETED_TABLES.isdisjoint(actual_tables)
    assert DELETED_BOT_COLUMNS.isdisjoint({column["name"] for column in inspector.get_columns("bots")})
    bot_plugin_columns = {column["name"] for column in inspector.get_columns("bot_plugins")}
    assert {"secret_config_encrypted", "secret_config_version"} <= bot_plugin_columns
    assert "plugin_state_records" in actual_tables
    assert "fun_command_rules" in actual_tables
    assert {
        "cultivation_clans",
        "cultivation_cooldowns",
        "cultivation_inventory",
        "cultivation_players",
        "cultivation_transactions",
    } <= actual_tables
    task_job_columns = {column["name"] for column in inspector.get_columns("task_jobs")}
    assert "dedupe_key" in task_job_columns
    # 平台大盘的跨租户查询必须命中明确的组合索引，不能依赖数据量小时的全表扫描表现。
    for table_name, index_name in PLATFORM_ANALYTICS_INDEXES.items():
        actual_indexes = {item["name"] for item in inspector.get_indexes(table_name)}
        assert index_name in actual_indexes
    migration_engine.dispose()


def test_database_revision_guard_accepts_head_and_rejects_stale_revision(tmp_path: Path) -> None:
    """业务进程只能在数据库版本与代码 head 完全一致时继续启动。"""

    database_path = tmp_path / "guard.db"
    _run_alembic(database_path, "upgrade", "head")

    from app.services.database_lifecycle import assert_database_at_head
    from app.services.database_lifecycle import expected_database_revision

    migration_engine = create_engine(f"sqlite:///{database_path}")
    with migration_engine.connect() as connection:
        assert_database_at_head(connection)

    # 模拟数据库落后于代码版本；错误同时包含当前值和期望值，便于部署时快速定位。
    with migration_engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'outdated'"))
    with migration_engine.connect() as connection:
        expected_revision = expected_database_revision()
        with pytest.raises(RuntimeError, match=f"database_revision_mismatch:outdated:{expected_revision}"):
            assert_database_at_head(connection)
    migration_engine.dispose()
