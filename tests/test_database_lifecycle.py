from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _upgrade_database(database_path: Path) -> None:
    """通过正式 Alembic 链路创建临时数据库，避免测试依赖应用启动补表。"""

    environment = os.environ.copy()
    environment.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": f"sqlite:///{database_path}",
            "DATA_DIR": str(database_path.parent / "data"),
            "LOG_DIR": str(database_path.parent / "logs"),
            "AUTH_SEED_DEMO_USERS": "false",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_initialization_only_checks_revision_and_seeds_data(tmp_path: Path, monkeypatch) -> None:
    """生产初始化不得调用 create_all 或运行时 ALTER，只能在 head 结构上写安全种子。"""

    from app.core.db import Base
    import app.services.init_db as lifecycle

    database_path = tmp_path / "head.db"
    _upgrade_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    calls: list[str] = []

    monkeypatch.setattr(Base.metadata, "create_all", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("create_all called")))
    monkeypatch.setattr(lifecycle, "seed_auth_users", lambda _db: calls.append("legacy_seed"))
    monkeypatch.setattr(lifecycle, "ensure_bootstrap_platform_admin", lambda _db: calls.append("platform_seed"))
    monkeypatch.setattr(lifecycle, "sync_plugins_from_directory", lambda _db: calls.append("plugins"))
    monkeypatch.setattr(lifecycle, "backfill_conversation_channels", lambda _db: calls.append("backfill"))
    lifecycle.reset_database_lifecycle_state()

    with factory() as db:
        lifecycle.init_db_with_seed(db)

    assert calls == ["legacy_seed", "platform_seed", "plugins", "backfill"]
    assert lifecycle.database_readiness()["ready"] is True
    engine.dispose()


def test_stale_revision_marks_not_ready_and_blocks_worker_start(tmp_path: Path, monkeypatch) -> None:
    """落后 revision 不启动 Worker，readyz 返回 503 并给出可诊断错误。"""

    import app.main as main_module
    import app.services.init_db as lifecycle

    database_path = tmp_path / "stale.db"
    _upgrade_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = 'outdated'"))
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    worker_calls: list[str] = []

    monkeypatch.setattr(main_module, "SessionLocal", factory)
    monkeypatch.setattr(main_module, "start_background_workers", lambda: worker_calls.append("started"))
    lifecycle.reset_database_lifecycle_state()
    asyncio.run(main_module.startup_event())

    readiness = lifecycle.database_readiness()
    assert readiness["ready"] is False
    assert "database_revision_mismatch:outdated:" in str(readiness["error"])
    assert worker_calls == []
    response = asyncio.run(main_module.readyz())
    assert response.status_code == 503
    assert json.loads(response.body)["ready"] is False
    engine.dispose()


def test_installer_creates_admin_only_after_migrations(tmp_path: Path) -> None:
    """安装服务只能在已迁移结构中创建平台管理员，空库调用不得隐式建表。"""

    from app.models.saas import PlatformUser
    from app.services.installer import provision_platform_admin

    empty_path = tmp_path / "empty.db"
    empty_engine = create_engine(f"sqlite:///{empty_path}")
    empty_factory = sessionmaker(bind=empty_engine, autoflush=False, autocommit=False)
    with empty_factory() as db:
        error = provision_platform_admin(
            db,
            admin_username="platform-admin",
            admin_password="Admin-secret-123",
            admin_display_name="平台管理员",
        )
    assert error
    assert inspect(empty_engine).get_table_names() == []
    empty_engine.dispose()

    migrated_path = tmp_path / "migrated.db"
    _upgrade_database(migrated_path)
    migrated_engine = create_engine(f"sqlite:///{migrated_path}")
    migrated_factory = sessionmaker(bind=migrated_engine, autoflush=False, autocommit=False)
    with migrated_factory() as db:
        error = provision_platform_admin(
            db,
            admin_username="platform-admin",
            admin_password="Admin-secret-123",
            admin_display_name="平台管理员",
        )
        assert error is None
        user = db.query(PlatformUser).filter_by(username="platform-admin").one()
        assert user.role == "platform_admin"
        assert user.must_change_password is True
    migrated_engine.dispose()


def test_deploy_script_migrates_before_stopping_old_process() -> None:
    """部署脚本先完成完整性、密钥、迁移和清理，再替换旧进程。"""

    script = (PROJECT_ROOT / "scripts" / "deploy_restart.sh").read_text(encoding="utf-8")
    catalog_index = script.index("scripts/verify_plugin_migration.py")
    cleanup_import_index = script.index("from app.services.plugin_secret_cleanup")
    secret_key_index = script.index("encrypt_plugin_secret_config")
    migration_index = script.index("alembic upgrade head")
    scrub_index = script.index("scripts/scrub_plugin_plaintext_secrets.py")
    stop_index = script.index("stopping old process")
    ready_index = script.index("/readyz")

    assert catalog_index < migration_index
    assert cleanup_import_index < migration_index
    assert secret_key_index < migration_index
    assert migration_index < scrub_index < stop_index < ready_index
    assert "MBS_DB_BACKUP_COMMAND" not in script
    assert "MBS_INITIAL_DEPLOY" not in script
    assert "':(exclude).rcodex'" in script
