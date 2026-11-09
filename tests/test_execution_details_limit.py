"""TEXT 列写入前的容量收口:回复里的图片 base64 不能撑爆执行日志和调度摘要。"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="details-limit-test-logs-"))

from app.core.text_column import BYTE_LIMIT, dump_json_for_text_column


def test_large_base64_reply_is_truncated_below_column_limit() -> None:
    huge = "A" * 900_000
    debug = {
        "duration_ms": 12,
        "auto_outbound": [{"payload": {"message_type": "image", "content": huge}}],
        "runtime_payload": {"replies": [{"type": "image", "content": huge}]},
    }
    text = dump_json_for_text_column({"decision_debug": {"matched_keyword": "douyin.com"}, "execution_debug": debug}, drop_keys=("runtime_payload",))
    assert len(text.encode("utf-8")) < BYTE_LIMIT
    parsed = json.loads(text)
    # 排查需要的小字段保留,大字符串被标注截断。
    assert parsed["decision_debug"]["matched_keyword"] == "douyin.com"
    assert parsed["execution_debug"]["duration_ms"] == 12
    assert "已截断" in parsed["execution_debug"]["auto_outbound"][0]["payload"]["content"]


def test_small_details_are_kept_verbatim() -> None:
    text = dump_json_for_text_column({"decision_debug": {"a": 1}, "execution_debug": {"duration_ms": 3, "output": {"replies": [{"content": "你好"}]}}})
    assert json.loads(text)["execution_debug"]["output"]["replies"][0]["content"] == "你好"


def test_unshrinkable_structure_falls_back_to_marker() -> None:
    """大量小字段累积超限且无可丢弃键时,兜底写截断标记而不是抛错。"""

    wide = {f"k{i}": "x" * 400 for i in range(400)}
    text = dump_json_for_text_column(wide)
    assert len(text.encode("utf-8")) <= BYTE_LIMIT
    assert json.loads(text) == {"truncated": True, "reason": "内容超过存储上限"}
