from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.tenant_context import TenantScopeError, tenant_scope
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, TaskJob
from app.models.saas import Tenant
from app.services import operation_feedback
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.operation_feedback import OperationFeedbackError, build_operation_status
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB
from app.services.wechat_webridge.contract import JOB_TYPE


@pytest.fixture()
def env(monkeypatch):
    current = now_shanghai()
    monkeypatch.setattr(operation_feedback, "now_shanghai", lambda: current)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db, tenant_scope(1):
        db.add_all([Tenant(id=n, code=f"tenant-{n}", name=f"企业{n}") for n in (1, 2)])
        db.add_all([Bot(id=n, tenant_id=n, name=f"机器人{n}") for n in (1, 2)])
        bridge = Channel(id=1, tenant_id=1, bot_id=1, name="微桥账号", channel_type="wechat_webridge")
        other = Channel(id=2, tenant_id=2, bot_id=2, name="其他企业账号", channel_type="wechat_webridge")
        cloud = Channel(id=3, tenant_id=1, bot_id=1, name="企微账号", channel_type="workwechat")
        db.add_all([bridge, other, cloud])
        db.commit()
        yield db, bridge, other, cloud, current
    engine.dispose()


def _job(db, channel, current, *, action="room_members", status="pending", result=None, age=10, **kwargs):
    job_type = JOB_TYPE if channel.channel_type == "wechat_webridge" else WORKWECHAT_CLOUD_CONTROL_JOB
    payload = {"channel_id": channel.id, "command": {"type": action}} if job_type == JOB_TYPE else {"channel_id": channel.id, "action": action}
    job = TaskJob(
        tenant_id=channel.tenant_id, job_type=job_type, status=status,
        created_at=current - timedelta(seconds=age), payload_json=json.dumps(payload),
        result_json=json.dumps(result) if result is not None else None, **kwargs,
    )
    db.add(job)
    db.commit()
    return job


def _webridge_result(**detail):
    return {"webridge": {"ok": True, "result": {"ok": True, **detail}}}


@pytest.mark.parametrize("job_ids", [None, [], (), [True], [0], [-1], ["1"], [1] * 201])
def test_rejects_invalid_job_ids(env, job_ids):
    db, bridge, *_ = env
    with pytest.raises(OperationFeedbackError) as error:
        build_operation_status(db, bridge, job_ids)
    assert error.value.status_code == 400


def test_requires_trusted_tenant_context(env):
    db, bridge, _, _, current = env
    job = _job(db, bridge, current)
    with tenant_scope(2), pytest.raises(OperationFeedbackError):
        build_operation_status(db, bridge, [job.id])
    from app.core.tenant_context import _CURRENT_TENANT_ID

    token = _CURRENT_TENANT_ID.set(None)
    try:
        with pytest.raises(TenantScopeError):
            build_operation_status(db, bridge, [job.id])
    finally:
        _CURRENT_TENANT_ID.reset(token)


def test_rejects_other_tenant_channel_and_job_type(env):
    db, bridge, other, cloud, current = env
    foreign = _job(db, other, current)
    cloud_job = _job(db, cloud, current, action="sync_contacts")
    for channel, ids in ((bridge, [foreign.id]), (other, [foreign.id]), (bridge, [cloud_job.id]), (cloud, [99999])):
        with pytest.raises(OperationFeedbackError) as error:
            build_operation_status(db, channel, ids)
        assert error.value.status_code == 404
    cloud_job.job_type = "plugin_dispatch"
    db.commit()
    with pytest.raises(OperationFeedbackError):
        build_operation_status(db, cloud, [cloud_job.id])


@pytest.mark.parametrize("channel_id", [True, "1", 2, None])
def test_payload_channel_must_be_strictly_the_requested_channel(env, channel_id):
    db, bridge, _, _, current = env
    job = _job(db, bridge, current)
    job.payload_json = json.dumps({"channel_id": channel_id, "command": {"type": "room_members"}})
    db.commit()
    with pytest.raises(OperationFeedbackError):
        build_operation_status(db, bridge, [job.id])


def test_reads_external_payload_through_shared_parser(env, monkeypatch):
    db, bridge, _, _, current = env
    job = _job(db, bridge, current)
    job.payload_json = '{"__storage__":"file"}'
    db.commit()
    monkeypatch.setattr(operation_feedback, "parse_job_payload", lambda value: {"channel_id": bridge.id, "command": {"type": "room_members"}})
    assert build_operation_status(db, bridge, [job.id, job.id])["jobs"][0]["state"] == "pending"
    assert len(build_operation_status(db, bridge, [job.id, job.id])["jobs"]) == 1


def test_two_jobs_wait_for_both_and_report_mixed_completion_without_private_details(env):
    db, _, _, cloud, current = env
    profile = _job(db, cloud, current, action="sync_chatrooms", status="completed", result={
        "action": "sync_chatrooms", "adapter_result": {"ok": True}, "total": 1,
    })
    members = _job(db, cloud, current, action="sync_chatroom_members")
    progress = build_operation_status(db, cloud, [profile.id, members.id])
    assert not progress["done"] and progress["successful"] == 1 and progress["failed"] == 0
    members.status, members.error_message = "failed", "secret-token-and-account-id"
    db.commit()
    progress = build_operation_status(db, cloud, [profile.id, members.id])
    assert progress["done"] and progress["successful"] == progress["failed"] == 1
    assert "secret-token-and-account-id" not in json.dumps(progress)


def test_pending_with_previous_error_is_still_waiting_for_retry(env):
    db, bridge, _, _, current = env
    job = _job(db, bridge, current, error_message="private-provider-error", attempts=1)
    progress = build_operation_status(db, bridge, [job.id])
    assert not progress["done"] and progress["failed"] == 0
    assert progress["jobs"][0]["state"] == "pending" and "重试" in progress["summary"]
    assert "private-provider-error" not in json.dumps(progress)


def test_status_read_does_not_flush_unrelated_pending_changes(env, monkeypatch):
    db, bridge, _, _, current = env
    job = _job(db, bridge, current)
    job_id = job.id
    bridge.name = "尚未保存的名称"

    def reject_flush(*args, **kwargs):
        pytest.fail("状态查询不应触发数据库写入")

    monkeypatch.setattr(db, "flush", reject_flush)
    assert build_operation_status(db, bridge, [job_id])["jobs"][0]["state"] == "pending"


@pytest.mark.parametrize("channel_name,status,age", [("bridge", "pending", 181), ("bridge", "running", 181), ("cloud", "pending", 301), ("cloud", "running", 301)])
def test_timeout_only_ends_page_waiting_without_mutating_task(env, channel_name, status, age):
    db, bridge, _, cloud, current = env
    channel = bridge if channel_name == "bridge" else cloud
    job = _job(db, channel, current, action="room_members" if channel is bridge else "sync_contacts", status=status, age=age)
    progress = build_operation_status(db, channel, [job.id])
    assert progress["done"] and progress["failed"] == 1 and progress["jobs"][0]["state"] == "timeout"
    assert not db.dirty and job.status == status
    db.expire_all()
    assert db.get(TaskJob, job.id).status == status


def test_cloud_expired_lease_stops_waiting_and_finished_elapsed_is_stable(env):
    db, _, _, cloud, current = env
    job = _job(db, cloud, current, action="sync_contacts", status="running", lease_expires_at=current - timedelta(seconds=1))
    assert build_operation_status(db, cloud, [job.id])["jobs"][0]["state"] == "timeout"
    job.status, job.finished_at = "completed", current - timedelta(seconds=4)
    job.result_json = json.dumps({"action": "sync_contacts", "adapter_result": {"ok": True}, "synced_contacts": 0})
    db.commit()
    row = build_operation_status(db, cloud, [job.id])["jobs"][0]
    assert row["state"] == "completed" and row["elapsed_seconds"] == 6


@pytest.mark.parametrize("result", [None, [], {}, {"webridge": []}, {"webridge": {"ok": True, "result": []}}, _webridge_result(members=[{"name": "missing-id"}]), _webridge_result(members="invalid")])
def test_completed_task_with_malformed_result_is_not_successful(env, result):
    db, bridge, _, _, current = env
    job = _job(db, bridge, current, status="completed", result=result)
    assert build_operation_status(db, bridge, [job.id])["jobs"][0]["state"] == "failed"
    job.result_json = "not-json"
    db.commit()
    assert build_operation_status(db, bridge, [job.id])["failed"] == 1


@pytest.mark.parametrize("ok", [False, 0, "false", "0", "true", None, [], {}])
@pytest.mark.parametrize("location", ["envelope", "detail", "cloud"])
def test_explicit_unsuccessful_flags_override_completed_state(env, ok, location):
    db, bridge, _, cloud, current = env
    if location == "cloud":
        channel, action = cloud, "sync_contacts"
        result = {"action": action, "adapter_result": {"ok": ok}, "synced_contacts": 2}
    else:
        channel, action, result = bridge, "room_members", _webridge_result(members=[])
        target = result["webridge"] if location == "envelope" else result["webridge"]["result"]
        target["ok"] = ok
    job = _job(db, channel, current, action=action, status="completed", result=result)
    assert build_operation_status(db, channel, [job.id])["failed"] == 1


@pytest.mark.parametrize("action,key", [("room_members", "members"), ("friends", "items"), ("friends_all", "contacts"), ("groups_all", "rooms")])
def test_empty_directory_is_a_valid_success(env, action, key):
    db, bridge, _, _, current = env
    job = _job(db, bridge, current, action=action, status="completed", result=_webridge_result(**{key: []}))
    progress = build_operation_status(db, bridge, [job.id])
    assert progress["done"] and progress["successful"] == 1 and "0 " in progress["summary"]


@pytest.mark.parametrize("recognized", [None, ["members"]])
def test_legacy_and_current_compacted_directory_results_use_persisted_counts(env, recognized):
    db, bridge, _, _, current = env
    detail = {"truncated": True, "persisted_counts": {"members": 307}}
    if recognized is not None:
        detail["directory_result_keys"] = recognized
    job = _job(db, bridge, current, status="completed", result=_webridge_result(**detail))
    progress = build_operation_status(db, bridge, [job.id])
    assert progress["successful"] == 1 and "307 位成员" in progress["summary"]


@pytest.mark.parametrize("extra", [{"action": "sync_chatrooms"}, {"synced_contacts": "3"}, {"synced_contacts": True}, {"synced_contacts": -1}, {"adapter_result": []}, {"adapter_result": {"result": {"ok": False}}}])
def test_cloud_completion_requires_matching_action_and_valid_result(env, extra):
    db, _, _, cloud, current = env
    result = {"action": "sync_contacts", "adapter_result": {"ok": True}, "synced_contacts": 3, **extra}
    job = _job(db, cloud, current, action="sync_contacts", status="completed", result=result)
    assert build_operation_status(db, cloud, [job.id])["failed"] == 1


@pytest.mark.parametrize("age,online,enabled,expected", [(20, True, True, True), (181, True, True, False), (20, False, True, False), (20, True, False, False)])
def test_health_check_matches_account_presence_without_exposing_device_identity(age, online, enabled, expected):
    channel = Channel(channel_type="wechat_webridge", is_enabled=enabled, runtime_info_json=json.dumps({
        "webridge": {"online": online, "last_seen_at": (now_shanghai() - timedelta(seconds=age)).isoformat(), "device_id": "private-device"},
    }))
    result = WechatWeBridgeAdapter().health_check(channel)
    assert result["ok"] is expected and "最近一次状态已登记" not in result["summary"]
    assert "private-device" not in json.dumps(result)
