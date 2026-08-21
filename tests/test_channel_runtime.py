from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.services.channel_runtime as channel_runtime
from app.core.db import Base
from app.core.tenant_context import get_current_tenant_id, tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel
from app.models.saas import SaasPlan, Tenant, TenantSubscription


@pytest.fixture()
def runtime_context(tmp_path, monkeypatch):
    """构造共享数据库中的两个有效租户，并把协调器会话绑定到隔离测试引擎。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'channel-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(channel_runtime, "SessionLocal", factory)
    monkeypatch.setattr(channel_runtime, "TenantSessionLocal", factory)
    monkeypatch.setattr(channel_runtime, "_next_due_at", {})

    with factory() as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        plan = SaasPlan(code="runtime", name="运行套餐", bot_limit=10, status="active")
        db.add_all([alpha, beta, plan])
        db.flush()
        current = now_shanghai()
        db.add_all(
            [
                TenantSubscription(
                    tenant_id=alpha.id,
                    plan_id=plan.id,
                    status="active",
                    starts_at=current,
                    expires_at=current + timedelta(days=30),
                ),
                TenantSubscription(
                    tenant_id=beta.id,
                    plan_id=plan.id,
                    status="active",
                    starts_at=current,
                    expires_at=current + timedelta(days=30),
                ),
            ]
        )
        db.commit()
        tenant_ids = (int(alpha.id), int(beta.id))

    yield factory, tenant_ids

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _create_channel(factory, tenant_id: int, *, channel_type: str, name: str, login_state: str = "unknown") -> int:
    """在指定租户会话内创建启用渠道，避免测试数据绕过真实租户写入约束。"""

    with tenant_scope(tenant_id), tenant_session(factory) as db:
        bot = Bot(name=f"{name}机器人")
        db.add(bot)
        db.flush()
        channel = Channel(
            bot_id=bot.id,
            name=name,
            channel_type=channel_type,
            is_enabled=True,
            status="online",
            login_state=login_state,
        )
        db.add(channel)
        db.commit()
        return int(channel.id)


def test_runtime_executes_each_channel_inside_its_tenant_scope(runtime_context) -> None:
    """平台只枚举租户主键，协议 runner 必须在与渠道归属一致的可信作用域内执行。"""

    factory, (alpha_id, beta_id) = runtime_context
    _create_channel(factory, alpha_id, channel_type="telegram", name="甲 Telegram")
    _create_channel(factory, beta_id, channel_type="telegram", name="乙 Telegram")
    executed: list[tuple[int | None, int, str]] = []

    def fake_telegram_runner(_db, channel):
        executed.append((get_current_tenant_id(), int(channel.tenant_id), channel.channel_type))
        return {"ok": True, "processed": 0}

    result = channel_runtime.run_due_channel_tasks(
        now_monotonic=10.0,
        runners={"telegram_poll": fake_telegram_runner},
    )

    assert executed == [
        (alpha_id, alpha_id, "telegram"),
        (beta_id, beta_id, "telegram"),
    ]
    assert result.worked is True
    assert result.attempted_tasks == 2
    assert result.failed_tasks == 0


def test_runtime_failure_does_not_block_later_tenant(runtime_context) -> None:
    """单个租户的协议异常只记录当前任务，后续租户仍必须得到执行机会。"""

    factory, (alpha_id, beta_id) = runtime_context
    _create_channel(factory, alpha_id, channel_type="telegram", name="甲失败渠道")
    _create_channel(factory, beta_id, channel_type="telegram", name="乙正常渠道")
    executed: list[int] = []

    def isolated_runner(_db, channel):
        tenant_id = int(get_current_tenant_id() or 0)
        executed.append(tenant_id)
        if tenant_id == alpha_id:
            raise RuntimeError("alpha_poll_failed")
        return {"ok": True, "processed": 1}

    result = channel_runtime.run_due_channel_tasks(
        now_monotonic=20.0,
        runners={"telegram_poll": isolated_runner},
    )

    assert executed == [alpha_id, beta_id]
    assert result.attempted_tasks == 2
    assert result.completed_tasks == 1
    assert result.failed_tasks == 1


def test_runtime_only_schedules_declared_platform_polling_tasks(runtime_context) -> None:
    """企业微信远端执行和 Windows Webhook 均不进入平台主动轮询，iPad 任务按独立间隔运行。"""

    factory, (alpha_id, beta_id) = runtime_context
    _create_channel(factory, alpha_id, channel_type="workwechat", name="企微远端")
    _create_channel(factory, alpha_id, channel_type="wechat_winauto", name="Windows Webhook")
    _create_channel(factory, beta_id, channel_type="wechat_ipad", name="iPad 在线", login_state="online")
    executed: list[tuple[str, int]] = []

    def record(task_name: str):
        def runner(_db, channel):
            executed.append((task_name, int(channel.tenant_id)))
            return {"ok": True, "skipped": False}

        return runner

    runners = {
        "wechat_ipad_message_sync": record("message_sync"),
        "wechat_ipad_heartbeat": record("heartbeat"),
        "wechat_ipad_auto_recover": record("auto_recover"),
    }
    first = channel_runtime.run_due_channel_tasks(now_monotonic=30.0, runners=runners)
    second = channel_runtime.run_due_channel_tasks(now_monotonic=31.0, runners=runners)
    third = channel_runtime.run_due_channel_tasks(now_monotonic=32.0, runners=runners)

    assert executed == [
        ("message_sync", beta_id),
        ("heartbeat", beta_id),
        ("message_sync", beta_id),
    ]
    assert first.attempted_tasks == 2
    assert second.attempted_tasks == 0
    assert third.attempted_tasks == 1


def test_runtime_skips_expired_tenant_subscription(runtime_context) -> None:
    """订阅到期后只保留数据读取能力，后台主动协议任务必须停止执行。"""

    factory, (alpha_id, beta_id) = runtime_context
    _create_channel(factory, alpha_id, channel_type="telegram", name="甲有效渠道")
    _create_channel(factory, beta_id, channel_type="telegram", name="乙到期渠道")
    with factory() as db:
        subscription = db.scalar(
            select(TenantSubscription)
            .where(TenantSubscription.tenant_id == beta_id)
            .order_by(TenantSubscription.id.desc())
        )
        subscription.expires_at = now_shanghai() - timedelta(minutes=1)
        db.commit()

    executed: list[int] = []

    def record_runner(_db, _channel):
        executed.append(int(get_current_tenant_id() or 0))
        return {"ok": True}

    result = channel_runtime.run_due_channel_tasks(
        now_monotonic=40.0,
        runners={"telegram_poll": record_runner},
    )

    assert executed == [alpha_id]
    assert result.skipped_tenants == 1
