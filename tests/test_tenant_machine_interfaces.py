from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.async_workers as async_workers
from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, Channel, OpenApiApp, TaskJob
from app.models.saas import Tenant
from app.services.open_api_auth import (
    OpenApiAuthError,
    authenticate_open_api_request,
    set_open_api_secret,
    sign_open_api_request,
)
from app.services.task_queue import OUTBOUND_DISPATCH_JOB, enqueue_job


@pytest.fixture()
def machine_db(tmp_path, monkeypatch):
    """为机器接口和 Worker 提供共享但严格分租户的临时数据库。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'tenant-machine.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(async_workers, "SessionLocal", factory)
    monkeypatch.setattr(async_workers, "TenantSessionLocal", factory)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _create_tenants(factory) -> tuple[int, int]:
    with factory() as db:
        alpha = Tenant(code="alpha", name="甲租户", status="active")
        beta = Tenant(code="beta", name="乙租户", status="active")
        db.add_all([alpha, beta])
        db.commit()
        db.refresh(alpha)
        db.refresh(beta)
        return int(alpha.id), int(beta.id)


def test_duplicate_app_key_requires_tenant_code(machine_db) -> None:
    """租户内唯一 app_key 在平台认证阶段必须通过租户编码消歧。"""

    alpha_id, beta_id = _create_tenants(machine_db)
    with machine_db() as db:
        for tenant_id in (alpha_id, beta_id):
            app = OpenApiApp(tenant_id=tenant_id, name="同名应用", app_key="shared", status="active")
            set_open_api_secret(app, "same-secret")
            db.add(app)
        db.commit()

        timestamp = str(int(time.time()))
        signature = sign_open_api_request("same-secret", b"{}", timestamp)
        with pytest.raises(OpenApiAuthError, match="tenant_code_required"):
            authenticate_open_api_request(
                db,
                headers={"X-App-Key": "shared", "X-Timestamp": timestamp, "X-Signature": signature},
                raw_body=b"{}",
            )

        result = authenticate_open_api_request(
            db,
            headers={
                "X-Tenant-Code": "alpha",
                "X-App-Key": "shared",
                "X-Timestamp": timestamp,
                "X-Signature": signature,
            },
            raw_body=b"{}",
        )
        assert result.tenant_id == alpha_id


def test_worker_rejects_cross_tenant_channel_before_provider_call(machine_db, monkeypatch) -> None:
    """任务 payload 混入其他租户渠道时必须在 Provider 调用前失败。"""

    alpha_id, beta_id = _create_tenants(machine_db)
    with tenant_scope(beta_id), tenant_session(machine_db) as db:
        beta_bot = Bot(name="乙机器人")
        db.add(beta_bot)
        db.flush()
        beta_channel = Channel(bot_id=beta_bot.id, name="乙企微", channel_type="workwechat")
        db.add(beta_channel)
        db.commit()
        beta_channel_id = int(beta_channel.id)

    with tenant_scope(alpha_id), tenant_session(machine_db) as db:
        job = enqueue_job(
            db,
            job_type=OUTBOUND_DISPATCH_JOB,
            payload={"channel_id": beta_channel_id, "conversation_id": 999, "payload": {"content": "越权"}},
            max_attempts=1,
        )
        db.commit()
        job_id = int(job.id)

    provider_called = False

    def forbidden_provider_call(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("跨租户任务不得调用 Provider")

    monkeypatch.setattr(async_workers, "dispatch_outbound_message", forbidden_provider_call)
    assert async_workers._run_single_job() is True
    assert provider_called is False

    with machine_db() as db:
        stored = db.get(TaskJob, job_id)
        assert stored.status == "failed"
        assert stored.error_message == "channel_not_found"
