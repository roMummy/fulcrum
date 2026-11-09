"""日志保留策略的轻量测试:默认 10M x 5 个文件,并清理超出的旧备份。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="log-rotation-logs-"))

from pathlib import Path

from app.core.logging import _prune_excess_backups
from app.core.settings import settings


def test_default_rotation_keeps_five_files_of_10m():
    assert settings.log_file_max_bytes == 10 * 1024 * 1024
    # 当前文件 + 4 个备份 = 5 个
    assert settings.log_file_backup_count == 4


def test_prune_removes_only_backups_beyond_keep(tmp_path: Path):
    for name in ("app.log", "app.log.1", "app.log.4", "app.log.5", "app.log.9", "error.log.6", "app.log.bak"):
        (tmp_path / name).write_text("x")
    _prune_excess_backups(tmp_path, "app.log", 4)
    left = {p.name for p in tmp_path.iterdir()}
    assert left == {"app.log", "app.log.1", "app.log.4", "error.log.6", "app.log.bak"}
