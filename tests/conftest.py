"""测试套件全局隔离：在任何 app 模块导入前锁定一次性运行目录。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "pytest"
TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
(TEST_RUNTIME / "logs").mkdir(parents=True, exist_ok=True)

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME)
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'test.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _reject_live_database() -> None:
    """测试引擎绝不能指向仓库内正在使用的 data/app.db。"""

    from app.core.db import engine

    if engine.url.get_backend_name() != "sqlite":
        return
    database = engine.url.database
    if not database:
        return
    assert Path(database).resolve() != (PROJECT_ROOT / "data" / "app.db").resolve(), (
        "测试引擎指向线上数据库 data/app.db，已中止"
    )
