"""执行日志 details_json 入库前的容量收口:回复里的图片 base64 不能撑爆 TEXT 列。"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="details-limit-test-logs-"))

from app.services.analytics import _DETAILS_BYTE_LIMIT, _dump_execution_details


def test_large_base64_reply_is_truncated_below_column_limit() -> None:
    huge = "A" * 900_000
    debug = {
        "duration_ms": 12,
        "auto_outbound": [{"payload": {"message_type": "image", "content": huge}}],
        "runtime_payload": {"replies": [{"type": "image", "content": huge}]},
    }
    text = _dump_execution_details({"matched_keyword": "douyin.com"}, debug)
    assert len(text.encode("utf-8")) < _DETAILS_BYTE_LIMIT
    parsed = json.loads(text)
    # 排查需要的小字段保留,大字符串被标注截断。
    assert parsed["decision_debug"]["matched_keyword"] == "douyin.com"
    assert parsed["execution_debug"]["duration_ms"] == 12
    assert "已截断" in parsed["execution_debug"]["auto_outbound"][0]["payload"]["content"]


def test_small_details_are_kept_verbatim() -> None:
    text = _dump_execution_details({"a": 1}, {"duration_ms": 3, "output": {"replies": [{"content": "你好"}]}})
    assert json.loads(text)["execution_debug"]["output"]["replies"][0]["content"] == "你好"
