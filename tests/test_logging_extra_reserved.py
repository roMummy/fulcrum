"""日志 extra 不能使用 LogRecord 保留字段(如 filename、name、message)。

保留字段会让 logger 抛 KeyError: "Attempt to overwrite ... in LogRecord"。
微桥媒体上传接口曾因此每次都返回 500,手机上报 server_upload_failed。
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def test_logger_extra_does_not_use_reserved_names() -> None:
    offenders: list[str] = []
    for base in ("app", "plugins"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                        for key in keyword.value.keys:
                            if isinstance(key, ast.Constant) and key.value in RESERVED:
                                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {key.value}")
    assert not offenders, offenders
