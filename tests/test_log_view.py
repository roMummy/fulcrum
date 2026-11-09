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


def test_read_page_reads_from_tail_and_paginates(tmp_path):
    from app.services.log_view import read_page

    lines = []
    for i in range(250):
        lines.append(f"2026-11-09 17:00:{i % 60:02d}.000 | INFO     | a.b:c:1 - job enqueued | job_id={i} job_type=t")
        if i == 100:
            lines += ["Traceback (most recent call last):", "ValueError: boom"]
    path = tmp_path / "app.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    first, more = read_page(path, page=1, page_size=100)
    assert len(first) == 100 and more is True
    assert first[0]["title"] == "任务入队 #249 (t)"  # 最新在前
    third, more = read_page(path, page=3, page_size=100)
    assert len(third) == 50 and more is False
    # 堆栈续行并入 job 100 这一条
    hit, _ = read_page(path, page=1, page_size=100, keyword="boom")
    assert len(hit) == 1 and hit[0]["trace"] == ["Traceback (most recent call last):", "ValueError: boom"]
