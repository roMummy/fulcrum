from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_RUNTIME = PROJECT_ROOT / ".run" / "test-runtime" / "plugin-scheduler"

os.environ["APP_ENV"] = "test"
os.environ["DATA_DIR"] = str(TEST_RUNTIME / "data")
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_RUNTIME / 'app.db'}"
os.environ["LOG_DIR"] = str(TEST_RUNTIME / "logs")
os.environ["AUTH_SEED_DEMO_USERS"] = "false"

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session, tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Plugin, TaskJob, WeatherSubscription
from app.models.saas import Tenant
from app.services.plugin_runtime.contracts import PluginExecutionResult
from app.services.plugin_runtime.scheduler import (
    PluginSchedulerError,
    enqueue_plugin_scheduled_action,
    process_plugin_scheduled_action_job,
    schedule_due_plugin_actions,
)
from app.services.task_queue import PLUGIN_SCHEDULED_ACTION_JOB, parse_job_payload


@pytest.fixture()
def scheduler_fixture(tmp_path: Path):
    """准备两个租户及多类统一调度绑定，验证归属与计划幂等。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'plugin-scheduler.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        daily_plugin = Plugin(code="daily_news", display_name="每日新闻", plugin_type="command")
        douyu_plugin = Plugin(code="douyu", display_name="斗鱼", plugin_type="command")
        dota_plugin = Plugin(code="dota2_stats", display_name="Dota 2 战绩", plugin_type="command")
        game_plugin = Plugin(code="game_task", display_name="游戏任务", plugin_type="command")
        summary_plugin = Plugin(code="message_summary", display_name="群聊总结", plugin_type="command")
        ranking_plugin = Plugin(code="daily_ranking", display_name="群聊每日排行", plugin_type="scheduled")
        member_context_plugin = Plugin(code="member_context", display_name="群成员画像", plugin_type="scheduled")
        value_rank_plugin = Plugin(code="value_rank", display_name="群成员身价", plugin_type="command")
        weather_plugin = Plugin(code="weather", display_name="天气", plugin_type="rule")
        db.add_all(
            [
                alpha,
                beta,
                daily_plugin,
                douyu_plugin,
                dota_plugin,
                game_plugin,
                summary_plugin,
                ranking_plugin,
                member_context_plugin,
                value_rank_plugin,
                weather_plugin,
            ]
        )
        db.flush()
        alpha_bot = Bot(tenant_id=alpha.id, name="甲机器人", status="running", is_enabled=True)
        beta_bot = Bot(tenant_id=beta.id, name="乙机器人", status="running", is_enabled=True)
        db.add_all([alpha_bot, beta_bot])
        db.flush()
        alpha_channel = Channel(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            name="甲渠道",
            channel_type="telegram",
            status="online",
            is_enabled=True,
        )
        db.add(alpha_channel)
        db.flush()
        conversation = Conversation(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            channel_id=alpha_channel.id,
            channel_type="telegram",
            external_id="room-alpha",
            chat_type="group",
        )
        second_conversation = Conversation(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            channel_id=alpha_channel.id,
            channel_type="telegram",
            external_id="room-alpha-second",
            chat_type="group",
        )
        db.add_all([conversation, second_conversation])
        db.flush()
        alpha_daily = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=daily_plugin.id,
            status="enabled",
            config_json=json.dumps({"push_time": "08:30", "last_push_date": ""}),
        )
        alpha_douyu = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=douyu_plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "poll_interval_seconds": 60,
                    "room_subscriptions": {"room-alpha": ["100"]},
                }
            ),
        )
        alpha_game = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=game_plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "random_task_enabled": True,
                    "random_task_time": "08:45",
                    "random_task_conversation_ids": [conversation.id, second_conversation.id],
                }
            ),
        )
        alpha_dota = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=dota_plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "dota2_group_ranking_conversation_ids": [conversation.id],
                    "group_ranking_prepare_time": "05:00",
                    "group_ranking_push_time": "09:00",
                }
            ),
        )
        alpha_summary = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=summary_plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "daily_summary_conversation_ids": [conversation.id],
                    "daily_summary_prepare_time": "03:00",
                    "daily_summary_push_time": "09:00",
                }
            ),
        )
        alpha_ranking = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=ranking_plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "daily_ranking_conversation_ids": [conversation.id],
                    "prepare_time": "04:30",
                    "push_time": "09:30",
                }
            ),
        )
        alpha_member_context = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=member_context_plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "member_context_conversation_ids": [conversation.id],
                    "daily_refresh_time": "04:20",
                    "weekly_refresh_time": "04:40",
                    "monthly_refresh_time": "04:50",
                }
            ),
        )
        alpha_value_rank = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=value_rank_plugin.id,
            status="enabled",
            config_json=json.dumps(
                {
                    "value_rank_conversation_ids": [conversation.id],
                    "daily_recompute_time": "04:00",
                    "weekly_report_prepare_time": "09:10",
                    "weekly_report_push_time": "09:35",
                }
            ),
        )
        alpha_weather = BotPlugin(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            plugin_id=weather_plugin.id,
            status="enabled",
            config_json=json.dumps({"daily_push_time": "08:00"}),
        )
        beta_daily = BotPlugin(
            tenant_id=beta.id,
            bot_id=beta_bot.id,
            plugin_id=daily_plugin.id,
            status="enabled",
            config_json=json.dumps({"push_time": "08:30", "last_push_date": ""}),
        )
        weather_subscription = WeatherSubscription(
            tenant_id=alpha.id,
            bot_id=alpha_bot.id,
            channel_id=alpha_channel.id,
            conversation_id=conversation.id,
            chat_type="group",
            target_external_id="room-alpha",
            subscriber_id="member-alpha",
            city_query="长沙",
            city_name="长沙",
            city_id="101250101",
            schedule_hour=8,
            status="active",
        )
        db.add_all(
            [
                alpha_daily,
                alpha_douyu,
                alpha_dota,
                alpha_game,
                alpha_summary,
                alpha_ranking,
                alpha_member_context,
                alpha_value_rank,
                alpha_weather,
                beta_daily,
                weather_subscription,
            ]
        )
        db.commit()
        identity = {
            "alpha_tenant_id": int(alpha.id),
            "alpha_bot_id": int(alpha_bot.id),
            "alpha_daily_binding_id": int(alpha_daily.id),
            "alpha_douyu_binding_id": int(alpha_douyu.id),
            "alpha_dota_binding_id": int(alpha_dota.id),
            "alpha_game_binding_id": int(alpha_game.id),
            "alpha_summary_binding_id": int(alpha_summary.id),
            "alpha_ranking_binding_id": int(alpha_ranking.id),
            "alpha_member_context_binding_id": int(alpha_member_context.id),
            "alpha_value_rank_binding_id": int(alpha_value_rank.id),
            "alpha_weather_binding_id": int(alpha_weather.id),
            "alpha_conversation_id": int(conversation.id),
            "alpha_second_conversation_id": int(second_conversation.id),
            "beta_tenant_id": int(beta.id),
            "beta_daily_binding_id": int(beta_daily.id),
        }
    try:
        yield factory, identity
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_scheduled_action_enqueue_is_idempotent_and_omits_tenant_payload(scheduler_fixture) -> None:
    """同一绑定、动作和计划时间只产生一个任务，租户归属只保存在任务表。"""

    factory, identity = scheduler_fixture
    scheduled_for = now_shanghai().replace(microsecond=0)
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, identity["alpha_daily_binding_id"])
        assert binding is not None
        first, first_created = enqueue_plugin_scheduled_action(
            db,
            binding,
            action_key="baidu_news_daily_push",
            scheduled_for=scheduled_for,
            high_side_effect=True,
        )
        second, second_created = enqueue_plugin_scheduled_action(
            db,
            binding,
            action_key="baidu_news_daily_push",
            scheduled_for=scheduled_for,
            high_side_effect=True,
        )
        db.commit()

        assert first_created is True
        assert second_created is False
        assert first.id == second.id
        assert first.max_attempts == 1
        assert first.dedupe_key == second.dedupe_key
        payload = parse_job_payload(first)
        assert set(payload) == {"binding_id", "action_key", "parameters"}
        assert "tenant_id" not in json.dumps(payload, ensure_ascii=False)

    with platform_session(factory) as db:
        jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == PLUGIN_SCHEDULED_ACTION_JOB)).all()
        assert len(jobs) == 1


def test_scheduler_allows_same_dedupe_shape_in_different_tenants(scheduler_fixture) -> None:
    """任务幂等键只在租户内唯一，不会让一个租户阻止另一个租户入队。"""

    factory, identity = scheduler_fixture
    scheduled_for = now_shanghai().replace(microsecond=0)
    for prefix in ("alpha", "beta"):
        with tenant_scope(identity[f"{prefix}_tenant_id"]), tenant_session(factory) as db:
            binding = db.get(BotPlugin, identity[f"{prefix}_daily_binding_id"])
            assert binding is not None
            job, created = enqueue_plugin_scheduled_action(
                db,
                binding,
                action_key="baidu_news_daily_push",
                scheduled_for=scheduled_for,
            )
            db.commit()
            assert created is True
            assert job.tenant_id == identity[f"{prefix}_tenant_id"]


def test_scheduler_rejects_identity_and_secret_parameters(scheduler_fixture) -> None:
    """任务业务参数不能携带平台身份或租户敏感配置。"""

    factory, identity = scheduler_fixture
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, identity["alpha_daily_binding_id"])
        assert binding is not None
        with pytest.raises(PluginSchedulerError):
            enqueue_plugin_scheduled_action(
                db,
                binding,
                action_key="baidu_news_daily_push",
                scheduled_for=now_shanghai(),
                parameters={"tenant_id": identity["beta_tenant_id"]},
            )
        with pytest.raises(PluginSchedulerError):
            enqueue_plugin_scheduled_action(
                db,
                binding,
                action_key="baidu_news_daily_push",
                scheduled_for=now_shanghai(),
                parameters={"provider": {"api_key": "forbidden"}},
            )


def test_due_schedule_scanner_creates_all_plugin_jobs_once(scheduler_fixture) -> None:
    """各类定时插件通过统一扫描器入队，重复扫描不重复创建。"""

    factory, identity = scheduler_fixture
    current = now_shanghai().replace(hour=10, minute=0, second=30, microsecond=0)
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        first = schedule_due_plugin_actions(db, now=current)
        second = schedule_due_plugin_actions(db, now=current + timedelta(seconds=5))
        db.commit()

        assert sorted(job.related_type for job in first) == [
            "plugin_action:baidu_news_daily_push",
            "plugin_action:daily_ranking_prepare",
            "plugin_action:daily_ranking_push",
            "plugin_action:daily_summary",
            "plugin_action:daily_summary_prepare",
            "plugin_action:dota2_group_ranking_prepare",
            "plugin_action:dota2_group_ranking_push",
            "plugin_action:member_context_daily_refresh",
            "plugin_action:poll_subscriptions",
            "plugin_action:random_task_assignment",
            "plugin_action:random_task_assignment",
            "plugin_action:value_rank_daily_recompute",
            "plugin_action:weather_daily_push",
        ]
        assert second == []
        attempts = {job.related_type: job.max_attempts for job in first}
        assert attempts["plugin_action:daily_summary_prepare"] == 3
        assert attempts["plugin_action:daily_summary"] == 1
        assert attempts["plugin_action:daily_ranking_prepare"] == 3
        assert attempts["plugin_action:daily_ranking_push"] == 1
        assert attempts["plugin_action:dota2_group_ranking_prepare"] == 3
        assert attempts["plugin_action:dota2_group_ranking_push"] == 1
        assert attempts["plugin_action:member_context_daily_refresh"] == 1
        assert attempts["plugin_action:value_rank_daily_recompute"] == 3
        assert attempts["plugin_action:random_task_assignment"] == 1
        game_jobs = [job for job in first if job.related_type == "plugin_action:random_task_assignment"]
        assert len(game_jobs) == 2
        assert {
            parse_job_payload(job)["conversation_id"] for job in game_jobs
        } == {
            identity["alpha_conversation_id"],
            identity["alpha_second_conversation_id"],
        }
        assert len({job.dedupe_key for job in game_jobs}) == 2
        summary_jobs = [job for job in first if "daily_summary" in str(job.related_type)]
        assert all(parse_job_payload(job)["conversation_id"] == identity["alpha_conversation_id"] for job in summary_jobs)
        assert all("messages" not in json.dumps(parse_job_payload(job), ensure_ascii=False) for job in summary_jobs)
        ranking_jobs = [job for job in first if "daily_ranking" in str(job.related_type)]
        assert len(ranking_jobs) == 2
        assert all(
            parse_job_payload(job)["conversation_id"] == identity["alpha_conversation_id"]
            for job in ranking_jobs
        )
        assert all("ranking" not in json.dumps(parse_job_payload(job)["parameters"], ensure_ascii=False) for job in ranking_jobs)
        dota_jobs = [job for job in first if "dota2_group_ranking" in str(job.related_type)]
        assert len(dota_jobs) == 2
        assert all(
            parse_job_payload(job)["conversation_id"] == identity["alpha_conversation_id"]
            for job in dota_jobs
        )
        member_context_jobs = [job for job in first if "member_context" in str(job.related_type)]
        assert len(member_context_jobs) == 1
        assert parse_job_payload(member_context_jobs[0])["conversation_id"] == identity["alpha_conversation_id"]


@pytest.mark.parametrize(
    ("year", "month", "day", "expected_actions"),
    [
        (
            2026,
            9,
            6,
            {
                "plugin_action:member_context_daily_refresh",
                "plugin_action:member_context_weekly_refresh",
            },
        ),
        (
            2026,
            9,
            30,
            {
                "plugin_action:member_context_daily_refresh",
                "plugin_action:member_context_monthly_refresh",
            },
        ),
    ],
)
def test_member_context_weekly_and_monthly_schedules(
    scheduler_fixture,
    year: int,
    month: int,
    day: int,
    expected_actions: set[str],
) -> None:
    """成员画像在周日和月末追加对应任务，并继续按会话独立幂等。"""

    factory, identity = scheduler_fixture
    current = now_shanghai().replace(
        year=year,
        month=month,
        day=day,
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    )
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        jobs = schedule_due_plugin_actions(db, now=current)
        db.commit()

        member_jobs = [job for job in jobs if "member_context" in str(job.related_type)]
        assert {str(job.related_type) for job in member_jobs} == expected_actions
        assert all(
            parse_job_payload(job)["conversation_id"] == identity["alpha_conversation_id"]
            for job in member_jobs
        )
        assert all(job.max_attempts == 1 for job in member_jobs)


def test_value_rank_monday_schedules_snapshot_and_weekly_report(scheduler_fixture) -> None:
    """周一为每个目标群创建日快照、周报预生成和周报推送任务。"""

    factory, identity = scheduler_fixture
    current = now_shanghai().replace(
        year=2026,
        month=9,
        day=7,
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    )
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        jobs = schedule_due_plugin_actions(db, now=current)
        db.commit()

        value_jobs = [job for job in jobs if "value_rank" in str(job.related_type)]
        assert {str(job.related_type) for job in value_jobs} == {
            "plugin_action:value_rank_daily_recompute",
            "plugin_action:value_rank_weekly_report_prepare",
            "plugin_action:value_rank_weekly_report_push",
        }
        attempts = {str(job.related_type): int(job.max_attempts) for job in value_jobs}
        assert attempts["plugin_action:value_rank_daily_recompute"] == 3
        assert attempts["plugin_action:value_rank_weekly_report_prepare"] == 3
        assert attempts["plugin_action:value_rank_weekly_report_push"] == 1
        assert all(
            parse_job_payload(job)["conversation_id"] == identity["alpha_conversation_id"]
            for job in value_jobs
        )


def test_scheduled_job_rebuilds_context_from_job_tenant_and_redacts_result(scheduler_fixture) -> None:
    """执行阶段忽略业务载荷身份，只使用任务和租户会话重建可信上下文。"""

    factory, identity = scheduler_fixture
    captured = {}
    with tenant_scope(identity["alpha_tenant_id"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, identity["alpha_daily_binding_id"])
        assert binding is not None
        job, _ = enqueue_plugin_scheduled_action(
            db,
            binding,
            action_key="baidu_news_daily_push",
            scheduled_for=now_shanghai(),
            conversation_id=identity["alpha_conversation_id"],
        )
        payload = parse_job_payload(job)

        def fake_execute(db_arg, binding_arg, action, *, payload, context):  # noqa: ANN001
            captured.update(
                {
                    "db": db_arg,
                    "binding_id": binding_arg.id,
                    "action": action,
                    "payload": payload,
                    "context": context,
                }
            )
            return PluginExecutionResult(
                ok=True,
                status="completed",
                message="scheduled",
                output={"count": 1, "api_key": "must-not-persist"},
            )

        with patch(
            "app.services.plugin_runtime.scheduler.execute_binding_scheduled_action",
            side_effect=fake_execute,
        ):
            result = process_plugin_scheduled_action_job(db, job, payload)

        assert captured["db"] is db
        assert captured["binding_id"] == identity["alpha_daily_binding_id"]
        assert captured["action"] == "baidu_news_daily_push"
        assert captured["payload"] == {}
        assert captured["context"].tenant_id == identity["alpha_tenant_id"]
        assert captured["context"].bot_id == identity["alpha_bot_id"]
        assert captured["context"].conversation_id == identity["alpha_conversation_id"]
        assert result["output"] == {"count": 1, "api_key": "[REDACTED]"}

        forged_payload = {**payload, "tenant_id": identity["beta_tenant_id"]}
        with pytest.raises(PluginSchedulerError):
            process_plugin_scheduled_action_job(db, job, forged_payload)


def test_async_worker_source_has_no_plugin_specific_pollers() -> None:
    """后台 Worker 只保留统一调度入口，不再维护斗鱼或日报轮询字典。"""

    source = (PROJECT_ROOT / "app/services/async_workers.py").read_text(encoding="utf-8")
    assert "_run_due_douyu_pollers" not in source
    assert "_run_due_daily_news_pollers" not in source
    assert "_last_douyu_poll_at" not in source
    assert "_last_daily_news_poll_at" not in source
    assert "WEATHER_PUSH_JOB" not in source
    assert "_process_weather_push_job" not in source
    assert "process_plugin_scheduled_action_job" in source
