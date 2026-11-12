"""插件清单 [[schedule]]:声明式定时计划的解析、校验与调度器的通用构造。"""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="schedule-manifest-logs-"))

import pytest

from app.core.settings import settings
from app.core.time import now_shanghai
from app.services import plugin_directory
from app.services.plugin_manifest import parse_manifest, read_schedules
from app.services.plugin_runtime import scheduler

BASE = """
[plugin]
code = "demo"
display_name = "示例"
[capability]
channel_types = ["simulator"]
chat_types = ["group"]
message_types = ["text"]
"""


def _read(body: str):
    return read_schedules(parse_manifest(BASE + body))


def test_no_schedule_means_nothing_scheduled() -> None:
    assert _read("") == ((), [])


def test_daily_and_interval_declarations_are_read() -> None:
    schedules, errors = _read('''
[[schedule]]
action = "daily_push"
kind = "daily"
time_key = "send_time"
default_time = "20:00"
enabled_key = "on"
[[schedule]]
action = "poll"
kind = "interval"
interval_key = "every"
default_interval = 300
min_interval = 60
max_interval = 3600
priority = 65
high_side_effect = false
''')
    assert errors == []
    daily, poll = schedules
    assert (daily.action, daily.kind, daily.time_key, daily.default_time, daily.enabled_key) == ("daily_push", "daily", "send_time", "20:00", "on")
    assert (poll.default_interval, poll.min_interval, poll.max_interval, poll.priority, poll.high_side_effect) == (300, 60, 3600, 65, False)


@pytest.mark.parametrize(
    "body, expected",
    [
        ('[[schedule]]\naction = "A-b"\nkind = "daily"\ndefault_time = "08:00"', "action"),
        ('[[schedule]]\naction = "x"\nkind = "weekly"', "kind"),
        ('[[schedule]]\naction = "x"\nkind = "daily"', "default_time"),
        ('[[schedule]]\naction = "x"\nkind = "daily"\ndefault_time = "25:00"', "default_time"),
        ('[[schedule]]\naction = "x"\nkind = "interval"', "default_interval"),
        ('[[schedule]]\naction = "x"\nkind = "interval"\ndefault_interval = 10', "间隔需满足"),
        ('[[schedule]]\naction = "x"\nkind = "interval"\ndefault_interval = 100\nmax_interval = 50', "间隔需满足"),
        ('[[schedule]]\naction = "x"\nkind = "daily"\ndefault_time = "08:00"\n[[schedule]]\naction = "x"\nkind = "daily"\ndefault_time = "09:00"', "重复"),
        ('[[schedule]]\naction = "x"\nkind = "daily"\ndefault_time = "08:00"\npriority = 999', "priority"),
        ('[[schedule]]\naction = "x"\nkind = "daily"\ndefault_time = "08:00"\nhigh_side_effect = "yes"', "high_side_effect"),
        ('[[schedule]]\naction = "x"\nkind = "daily"\ndefault_time = "08:00"\ngrace_seconds = 5', "grace_seconds"),
    ],
)
def test_bad_declarations_make_the_plugin_invalid_with_a_reason(body, expected) -> None:
    _schedules, errors = _read("\n" + body)
    assert any(expected in message for message in errors), errors


def test_an_invalid_schedule_invalidates_the_discovered_plugin(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "plugins_dir", str(tmp_path))
    plugin_directory._SCAN_CACHE.clear()
    folder = tmp_path / "demo"
    folder.mkdir()
    (folder / "config.toml").write_text(BASE + '[[schedule]]\naction = "x"\nkind = "daily"\n', encoding="utf-8")
    (folder / "plugin.py").write_text("def execute_action(request):\n    return None\n", encoding="utf-8")
    (item,) = plugin_directory.discover_plugins(force=True)
    assert not item.is_valid and any("default_time" in message for message in item.validation_errors)
    plugin_directory._SCAN_CACHE.clear()


def _specs(schedules, config, current):
    return scheduler._build_declared_schedule_specs(schedules, config, current)


def test_scheduler_builds_daily_and_interval_specs_from_the_declaration() -> None:
    (daily,), _ = _read('[[schedule]]\naction = "d"\nkind = "daily"\ntime_key = "t"\ndefault_time = "20:00"\nenabled_key = "on"')
    (poll,), _ = _read('[[schedule]]\naction = "p"\nkind = "interval"\ninterval_key = "n"\ndefault_interval = 300\nmin_interval = 60\nmax_interval = 3600')
    day = now_shanghai().replace(hour=21, minute=30, second=0, microsecond=0)

    at = day.replace(hour=20, minute=0)
    assert _specs((daily,), {}, at + timedelta(seconds=10)) == ()          # 开关没打开
    (spec,) = _specs((daily,), {"on": True}, at + timedelta(seconds=10))
    assert spec.action_key == "d" and (spec.scheduled_for.hour, spec.scheduled_for.minute) == (20, 0)  # 缺省 20:00
    assert _specs((daily,), {"on": True}, at - timedelta(seconds=1)) == ()  # 还没到时间
    assert _specs((daily,), {"on": True}, at + timedelta(seconds=119))[0].scheduled_for == spec.scheduled_for  # 窗口内重复扫描:同一时刻,幂等
    assert _specs((daily,), {"on": True}, at + timedelta(seconds=120)) == ()  # 过了窗口:不补发(例如服务刚启动)
    assert _specs((daily,), {"on": True}, day) == ()                        # 21:30 才启动:错过 20:00,当天不再触发
    (custom,) = _specs((daily,), {"on": True, "t": "21:15"}, day.replace(minute=15, second=30))
    assert (custom.scheduled_for.hour, custom.scheduled_for.minute) == (21, 15)

    (bucket,) = _specs((poll,), {"n": 10}, day)                             # 间隔被钳制到下限 60 秒
    assert int(bucket.scheduled_for.timestamp()) % 60 == 0
    (bucket,) = _specs((poll,), {}, day)                                    # 缺省 300 秒
    assert int(bucket.scheduled_for.timestamp()) % 300 == 0


def test_bundled_idiom_guess_is_picked_up_without_any_scheduler_code() -> None:
    from pathlib import Path

    plugin_directory._SCAN_CACHE.clear()
    settings.plugins_dir = str(Path(__file__).resolve().parents[1] / "plugins")
    try:
        assert "idiom_guess" in scheduler._declared_schedule_codes()
        assert "idiom_guess" not in scheduler.LEGACY_SCHEDULED_PLUGIN_CODES
        (item,) = scheduler._declared_schedules("idiom_guess")
        assert (item.action, item.kind, item.time_key, item.enabled_key) == ("idiom_daily_start", "daily", "send_time", "scheduled_enabled")
    finally:
        plugin_directory._SCAN_CACHE.clear()
