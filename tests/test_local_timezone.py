from __future__ import annotations

import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.time import ensure_shanghai_datetime, format_shanghai_time, now_shanghai
from app.models.entities import Bot, TaskJob
from app.services.task_queue import claim_next_job, complete_job, enqueue_job


class LocalTimezoneTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        self.db = TestingSession()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_now_uses_system_local_timezone(self):
        current = now_shanghai()

        self.assertIsNotNone(current.tzinfo)
        self.assertEqual(current.utcoffset(), datetime.now().astimezone().utcoffset())

    def test_naive_datetime_is_treated_as_local_time(self):
        value = datetime(2026, 6, 7, 10, 30, 0)

        normalized = ensure_shanghai_datetime(value)

        self.assertEqual(format_shanghai_time(normalized), "2026-06-07 10:30:00")

    def test_model_default_created_at_uses_local_time(self):
        before = now_shanghai().replace(tzinfo=None)
        # 时区测试关注时间字段，机器人夹具按精简契约只提供名称。
        bot = Bot(name="Bot")
        self.db.add(bot)
        self.db.commit()
        after = now_shanghai().replace(tzinfo=None)

        created_at = bot.created_at.replace(tzinfo=None)
        self.assertGreaterEqual(created_at, before)
        self.assertLessEqual(created_at, after)

    def test_task_queue_times_use_local_time(self):
        before = now_shanghai().replace(tzinfo=None)
        job = enqueue_job(self.db, job_type="demo", payload={"ok": True})
        self.db.commit()
        claimed = claim_next_job(self.db, job_types=("demo",))
        self.assertIsNotNone(claimed)
        complete_job(self.db, claimed)
        after = now_shanghai().replace(tzinfo=None)

        refreshed = self.db.get(TaskJob, job.id)
        self.assertGreaterEqual(refreshed.available_at.replace(tzinfo=None), before)
        self.assertGreaterEqual(refreshed.started_at.replace(tzinfo=None), before)
        self.assertGreaterEqual(refreshed.finished_at.replace(tzinfo=None), before)
        self.assertLessEqual(refreshed.finished_at.replace(tzinfo=None), after)

    def test_explicit_utc_timestamp_still_formats_to_local_time(self):
        value = datetime(2026, 6, 7, 2, 30, 0, tzinfo=timezone.utc)

        self.assertEqual(format_shanghai_time(value), "2026-06-07 10:30:00")
