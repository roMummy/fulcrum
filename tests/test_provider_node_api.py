from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.api.workwechat_provider_cloud as provider_api
from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Conversation, TaskJob
from app.models.saas import ProviderNodeAccount, SaasPlan, Tenant, TenantSubscription
from app.services.provider_nodes import activate_provider_node, create_provider_node, issue_activation_code
from app.services.task_queue import enqueue_job
from app.services.workwechat_cloud_poll import resolve_outbound_job_type


@pytest.fixture()
def provider_api_context(tmp_path, monkeypatch):
    """节点 API 使用真实设备凭证和租户会话，但不启动完整主应用。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider-node-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(provider_api, "SessionLocal", factory)
    monkeypatch.setattr(provider_api, "TenantSessionLocal", factory)

    with factory() as db:
        tenant = Tenant(code="node-api", name="节点 API 租户", status="active")
        plan = SaasPlan(code="node-api-plan", name="节点 API 套餐", provider_node_limit=3, status="active")
        db.add_all([tenant, plan])
        db.flush()
        subscription = TenantSubscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            status="active",
            starts_at=now_shanghai(),
            expires_at=now_shanghai() + timedelta(days=30),
        )
        db.add(subscription)
        db.commit()
        ids = {"tenant": int(tenant.id), "subscription": int(subscription.id)}

    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        node = create_provider_node(db, ids["tenant"], name="客服电脑A")
        issued = issue_activation_code(db, node.id)
        ids["node"] = int(node.id)
        ids["activation_code"] = issued.raw_code

    app = FastAPI()
    app.include_router(provider_api.provider_router)
    with TestClient(app) as client:
        yield client, factory, ids

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _activate(client: TestClient, ids: dict) -> str:
    response = client.post(
        "/api/provider/activate",
        json={
            "activation_code": ids["activation_code"],
            "fingerprint": "device-fingerprint-a",
            "client_version": "2.0.0",
        },
    )
    assert response.status_code == 200
    return response.json()["device_token"]


def _heartbeat(client: TestClient, token: str, accounts: list[dict]) -> None:
    response = client.post(
        "/api/provider/heartbeat",
        headers={"X-Provider-Device-Token": token},
        json={
            "client_version": "2.0.0",
            "hook_status": "running",
            "workwechat_status": "running",
            "accounts": accounts,
        },
    )
    assert response.status_code == 200


def _bind_channels_and_enqueue(factory, tenant_id: int, node_id: int) -> list[int]:
    """把两个企微渠道绑定到同节点不同账号，并分别入队一条任务。"""

    with tenant_scope(tenant_id), tenant_session(factory) as db:
        accounts = db.scalars(
            select(ProviderNodeAccount)
            .where(ProviderNodeAccount.provider_node_id == node_id)
            .order_by(ProviderNodeAccount.account_key.asc())
        ).all()
        assert [account.account_key for account in accounts] == ["account-a", "account-b"]
        bot = Bot(name="节点 API 机器人")
        db.add(bot)
        db.flush()
        job_ids: list[int] = []
        for index, account in enumerate(accounts, start=1):
            channel = Channel(
                bot_id=bot.id,
                name=f"企微渠道{index}",
                channel_type="workwechat",
                adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll"}),
                provider_node_id=node_id,
                provider_account_id=account.id,
            )
            db.add(channel)
            db.flush()
            conversation = Conversation(
                bot_id=bot.id,
                channel_id=channel.id,
                channel_type="workwechat",
                external_id=f"room-{index}",
                title=f"测试群{index}",
                chat_type="group",
            )
            db.add(conversation)
            db.flush()
            job = enqueue_job(
                db,
                job_type=resolve_outbound_job_type(channel),
                payload={
                    "channel_id": channel.id,
                    "conversation_id": conversation.id,
                    "payload": {"message_type": "text", "content": f"消息{index}"},
                },
            )
            job_ids.append(int(job.id))
        db.commit()
        return job_ids


def test_activation_rejects_client_tenant_or_channel_and_returns_only_device_identity(provider_api_context) -> None:
    """激活码本身决定租户和节点，客户端不能提交替代身份。"""

    client, _factory, ids = provider_api_context
    rejected = client.post(
        "/api/provider/activate",
        json={
            "activation_code": ids["activation_code"],
            "fingerprint": "device-a",
            "tenant_id": 999,
        },
    )
    assert rejected.status_code == 400

    token = _activate(client, ids)
    assert token.startswith("pdev_")
    payload = client.post(
        "/api/provider/heartbeat",
        headers={"X-Provider-Device-Token": token},
        json={"accounts": []},
    ).json()
    assert payload["node_id"] == ids["node"]
    assert "tenant_id" not in payload


def test_one_device_polls_multiple_bound_accounts_and_completion_is_idempotent(provider_api_context) -> None:
    """节点设备可以拉取本节点多个账号任务，并用租约完成后安全重复回报。"""

    client, factory, ids = provider_api_context
    token = _activate(client, ids)
    _heartbeat(
        client,
        token,
        [
            {"account_key": "account-a", "display_name": "企微甲", "status": "online"},
            {"account_key": "account-b", "display_name": "企微乙", "status": "online"},
        ],
    )
    _bind_channels_and_enqueue(factory, ids["tenant"], ids["node"])

    poll = client.post(
        "/api/provider/poll",
        headers={"X-Provider-Device-Token": token},
        json={"batch_size": 5},
    )
    assert poll.status_code == 200
    jobs = poll.json()["jobs"]
    assert {job["provider_account_key"] for job in jobs} == {"account-a", "account-b"}
    assert all(job["lease_token"].startswith("please_") for job in jobs)

    first = jobs[0]
    completed = client.post(
        f"/api/provider/jobs/{first['job_id']}/complete",
        headers={"X-Provider-Device-Token": token},
        json={"lease_token": first["lease_token"], "adapter_result": {"ok": True, "status": "sent"}},
    )
    repeated = client.post(
        f"/api/provider/jobs/{first['job_id']}/complete",
        headers={"X-Provider-Device-Token": token},
        json={"lease_token": first["lease_token"], "adapter_result": {"ok": True, "status": "sent"}},
    )
    assert completed.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["idempotent"] is True

    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        stored = db.get(TaskJob, first["job_id"])
        assert stored is not None
        # 租约原文只用于本次回报鉴权，数据库只能保存领取阶段写入的摘要；
        # 完成结果不得因为复用整个请求体而把 lease_token 再写进 result_json。
        assert first["lease_token"] not in str(stored.result_json or "")
        assert "lease_token" not in json.loads(stored.result_json or "{}").get("cloud_result", {})


def test_other_node_cannot_complete_job_and_expired_lease_is_rejected(provider_api_context) -> None:
    """同租户其他节点也不能回报不属于自己的任务，过期租约必须拒绝。"""

    client, factory, ids = provider_api_context
    token = _activate(client, ids)
    _heartbeat(client, token, [{"account_key": "account-a", "status": "online"}, {"account_key": "account-b", "status": "online"}])
    _bind_channels_and_enqueue(factory, ids["tenant"], ids["node"])
    job = client.post(
        "/api/provider/poll",
        headers={"X-Provider-Device-Token": token},
        json={"batch_size": 1},
    ).json()["jobs"][0]

    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        other_node = create_provider_node(db, ids["tenant"], name="客服电脑B")
        other_code = issue_activation_code(db, other_node.id).raw_code
    with factory() as db:
        other = activate_provider_node(db, other_code, fingerprint="device-b")

    wrong_node = client.post(
        f"/api/provider/jobs/{job['job_id']}/complete",
        headers={"X-Provider-Device-Token": other.raw_device_token},
        json={"lease_token": job["lease_token"], "adapter_result": {"ok": True}},
    )
    assert wrong_node.status_code == 404

    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        stored = db.get(TaskJob, job["job_id"])
        stored.lease_expires_at = now_shanghai() - timedelta(seconds=1)
        db.commit()
    expired = client.post(
        f"/api/provider/jobs/{job['job_id']}/complete",
        headers={"X-Provider-Device-Token": token},
        json={"lease_token": job["lease_token"], "adapter_result": {"ok": True}},
    )
    assert expired.status_code == 409
    assert expired.json()["detail"] == "lease_expired"


def test_expired_tenant_can_heartbeat_but_poll_returns_no_new_jobs(provider_api_context) -> None:
    """租户到期后节点仍可上报状态，云端不再签发新任务租约。"""

    client, factory, ids = provider_api_context
    token = _activate(client, ids)
    with factory() as db:
        subscription = db.get(TenantSubscription, ids["subscription"])
        subscription.expires_at = now_shanghai() - timedelta(seconds=1)
        db.commit()

    heartbeat = client.post(
        "/api/provider/heartbeat",
        headers={"X-Provider-Device-Token": token},
        json={"accounts": []},
    )
    poll = client.post(
        "/api/provider/poll",
        headers={"X-Provider-Device-Token": token},
        json={"batch_size": 5},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["poll_allowed"] is False
    assert poll.status_code == 200
    assert poll.json()["read_only"] is True
    assert poll.json()["jobs"] == []
