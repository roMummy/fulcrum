"""在独立解释器中验证测试启动配置，避免同一进程环境掩盖数据库覆盖问题。"""

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = (
    "import runpy,os,json; runpy.run_path('tests/conftest.py'); "
    "print(json.dumps({key:os.environ[key] for key in ('DATA_DIR','DATABASE_URL','LOG_DIR')}))"
)


def _boot(**overrides):
    # 子进程不继承当前 pytest 的隔离地址，每次只使用本用例明确传入的配置。
    env = {key: value for key, value in os.environ.items()
        if key not in {"APP_ENV", "DATA_DIR", "DATABASE_URL", "LOG_DIR"}}
    return subprocess.run([sys.executable, "-c", BOOTSTRAP], cwd=ROOT,
        env={**env, **overrides}, text=True, capture_output=True)


def test_explicit_parallel_test_databases_remain_distinct(tmp_path):
    urls = []
    for name in ("worker-a", "worker-b"):
        directory = tmp_path / name
        url = f"sqlite:///{directory / 'test.db'}"
        result = _boot(APP_ENV="test", DATA_DIR=str(directory), DATABASE_URL=url, LOG_DIR=str(directory / "logs"))
        assert result.returncode == 0, result.stderr
        config = json.loads(result.stdout)
        assert config["DATABASE_URL"] == url and config["DATA_DIR"] == str(directory)
        urls.append(config["DATABASE_URL"])
    assert urls[0] != urls[1]


def test_unspecified_runs_receive_unique_test_directories():
    first, second = _boot(), _boot()
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout)["DATABASE_URL"] != json.loads(second.stdout)["DATABASE_URL"]


def test_live_database_configuration_is_rejected_before_app_import():
    result = _boot(APP_ENV="test", DATA_DIR=str(ROOT / "data"), DATABASE_URL=f"sqlite:///{ROOT / 'data/app.db'}")
    assert result.returncode != 0 and "测试" in result.stderr
