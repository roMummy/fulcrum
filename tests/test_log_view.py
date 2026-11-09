"""日志展示层解析与中文化的轻量测试。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="log-view-logs-"))

from app.services.log_view import parse_entries

INFO = "2026-11-09 17:18:52.509 | INFO     | app.services.task_queue:enqueue_job:85 - job enqueued | job_id=5431 job_type=outbound"
ERR = "2026-11-09 17:18:53.000 | ERROR    | app.x:run:1 - directory handler execution failed | plugin_code=video error=boom x"


def test_parse_translates_and_extracts_fields():
    entries = parse_entries([INFO])
    assert entries[0]["title"] == "任务入队 #5431 (outbound)"
    assert entries[0]["level"] == "INFO"
    # 标题已用到的字段不重复展示
    assert entries[0]["fields"] == []


def test_traceback_lines_attach_to_previous_entry():
    entries = parse_entries([ERR, "Traceback (most recent call last):", '  File "x.py", line 1'])
    assert len(entries) == 1
    assert entries[0]["title"] == "插件执行异常 video"
    assert entries[0]["fields"] == [("错误", "boom x")]
    assert len(entries[0]["trace"]) == 2
    assert "Traceback" in entries[0]["raw"]


def test_unknown_event_keeps_original_text():
    line = "2026-11-09 17:00:00.000 | WARNING  | a.b:c:1 - some new event | channel_id=2"
    entry = parse_entries([line])[0]
    assert entry["title"] == "some new event"
    assert entry["fields"] == [("渠道", "2")]
