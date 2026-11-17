"""测试套件全局隔离：在任何 app 模块导入前锁定一次性运行目录。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_ROOTS = (Path(tempfile.gettempdir()).resolve(), (PROJECT_ROOT / ".run" / "test-runtime").resolve())


def _require_test_path(value: str | Path) -> Path:
    """只允许临时目录或仓库专用测试目录，解析符号链接后再次检查真实归属。"""

    path = Path(value).resolve()
    # 即使仓库本身放在 /tmp，也不能把业务 data 目录当成临时测试目录。
    live_root = (PROJECT_ROOT / "data").resolve()
    if path == live_root or path.is_relative_to(live_root):
        raise RuntimeError("测试禁止访问仓库业务数据目录")
    if not any(path != root and path.is_relative_to(root) for root in TEST_ROOTS):
        raise RuntimeError(f"测试路径必须位于独立临时目录：{path}")
    return path


def _require_test_database(value: str) -> None:
    """测试禁止连接真实数据库服务；文件 SQLite 也必须落在受控测试目录。"""

    url = make_url(value)
    if url.get_backend_name() != "sqlite":
        raise RuntimeError("测试仅允许受控临时 SQLite 数据库")
    if url.database not in (None, "", ":memory:"):
        _require_test_path(url.database)


# 隔离脚本已为每个文件分配独立目录，必须原样保留；普通 pytest 每次创建新目录。
# 在导入任何 app 模块之前拒绝不受控地址，防止自动建表或测试清表误触业务数据。
configured_database = os.environ.get("DATABASE_URL")
if configured_database:
    if os.environ.get("APP_ENV") != "test":
        raise RuntimeError("显式测试数据库必须同时声明 APP_ENV=test")
    _require_test_database(configured_database)
TEST_RUNTIME = _require_test_path(os.environ.get("DATA_DIR") or tempfile.mkdtemp(prefix="fulcrum-pytest-"))
TEST_RUNTIME.mkdir(parents=True, exist_ok=True)
test_logs = _require_test_path(os.environ.get("LOG_DIR") or TEST_RUNTIME / "logs")
test_logs.mkdir(parents=True, exist_ok=True)

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME)
os.environ["DATABASE_URL"] = configured_database or f"sqlite:///{TEST_RUNTIME / 'test.db'}"
os.environ["LOG_DIR"] = str(test_logs)
os.environ["AUTH_SEED_DEMO_USERS"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _reject_live_database() -> None:
    """测试模块若在收集时重设环境，最终引擎仍必须通过同样的临时库归属检查。"""

    from app.core.db import engine

    _require_test_database(str(engine.url))
