"""同步监控只展示真实完成结果，并保护任务与企业边界。"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob
from app.services.wechat_webridge import directory_sync, service
from app.services.wechat_webridge.contract import JOB_TYPE
from test_directory_sync import sync_context  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


@pytest.fixture()
def monitor_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'monitor.db'}")
    Channel.__table__.create(engine)
    TaskJob.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add_all([
            Channel(id=1, tenant_id=1, bot_id=1, name="同步一号", channel_type="wechat_webridge"),
            Channel(id=2, tenant_id=2, bot_id=2, name="其他企业", channel_type="wechat_webridge"),
            Channel(id=3, tenant_id=1, bot_id=1, name="其他协议", channel_type="wechat_ipad"),
            Channel(id=4, tenant_id=1, bot_id=1, name="删除账号", channel_type="wechat_webridge", deleted_at=now_shanghai()),
            TaskJob(id=100, tenant_id=2, job_type=JOB_TYPE, status="pending", payload_json='{"channel_id":2,"command":{"type":"friends_all"}}'),
        ])
        db.commit()
    with tenant_scope(1), tenant_session(factory) as db:
        yield db
    engine.dispose()


def _job(db, *, kind="contacts", status="pending", channel_id=1, tenant_id=1, outcome=None, **fields):
    job = TaskJob(tenant_id=tenant_id, job_type=JOB_TYPE, status=status,
                  payload_json=json.dumps({"channel_id": channel_id, "command": {"type": "friends_all" if kind == "contacts" else "groups_all", "params": {}}}),
                  result_json=json.dumps({"webridge": outcome}) if outcome is not None else None, **fields)
    db.add(job)
    db.commit()
    return job


def test_pending_running_and_aggregate_are_real_task_states(monitor_db):
    first = _job(monitor_db)
    second = _job(monitor_db, status="running", locked_at=now_shanghai())
    body = directory_sync.sync_status(monitor_db, [first.id, second.id], "contacts")
    assert [job["state"] for job in body["jobs"]] == ["pending", "running"]
    assert body["jobs"][0]["message"] == "等待手机领取"
    assert body["done"] is False and body["successful"] == body["failed"] == 0


@pytest.mark.parametrize("kind,result,expected", [
    ("contacts", {"items": [{"account_id": "one"}, {"wxid": "two"}, {}, "invalid"]}, 2),
    ("contacts", {"friends": []}, 0),
    ("contacts", {"contacts": [{"username": "one"}]}, 1),
    ("chatrooms", {"groups": [{"room_id": "one@chatroom"}]}, 1),
    ("chatrooms", {"rooms": []}, 0),
    ("contacts", {"truncated": True, "directory_result_keys": ["items"], "persisted_counts": {"contacts": 5000}}, 5000),
    ("chatrooms", {"truncated": True, "directory_result_keys": ["groups"], "persisted_counts": {"groups": 350}}, 350),
    ("contacts", {"truncated": True, "directory_result_keys": ["friends"], "persisted_counts": {"contacts": 0}}, 0),
])
def test_finished_count_supports_empty_large_and_protocol_lists(monitor_db, kind, result, expected):
    job = _job(monitor_db, kind=kind, status="completed", outcome={"ok": True, "result": result}, finished_at=now_shanghai())
    body = directory_sync.sync_status(monitor_db, [job.id], kind)
    assert body["done"] and body["successful"] == 1 and body["failed"] == 0
    assert body["count"] == expected and body["jobs"][0]["count"] == expected


@pytest.mark.parametrize("outcome", [
    {}, {"ok": True, "result": {}}, {"ok": False, "result": {"items": []}},
    {"ok": "false", "result": {"items": []}},
    {"ok": True, "result": {"ok": False, "items": []}},
    {"ok": True, "error": "失败", "result": {"items": []}},
    {"ok": True, "result": {"error": "失败", "items": []}},
    {"ok": True, "command_name": "groups_all", "result": {"items": []}},
    {"ok": True, "result": {"ok": True, "result": {"items": []}}},
    {"ok": True, "result": {"persisted_counts": {"contacts": True}}},
    {"ok": True, "result": {"persisted_counts": {"contacts": 5000}}},
    {"ok": True, "result": {"items": [{}, "invalid"]}},
    {"ok": True, "result": {"truncated": True, "persisted_counts": {"contacts": 0}}},
    {"ok": True, "result": {"truncated": True, "directory_result_keys": [], "persisted_counts": {"contacts": 0}}},
    {"ok": True, "result": {"truncated": True, "directory_result_keys": ["groups"], "persisted_counts": {"contacts": 0}}},
])
def test_unknown_or_failed_plugin_response_never_reports_success(monitor_db, outcome):
    job = _job(monitor_db, status="completed", outcome=outcome)
    body = directory_sync.sync_status(monitor_db, [job.id], "contacts")
    assert body["done"] and body["successful"] == 0 and body["failed"] == 1
    assert body["jobs"][0]["state"] == "failed" and body["jobs"][0]["count"] is None


@pytest.mark.parametrize("status", ["pending", "running"])
def test_monitor_timeout_does_not_change_job_or_overwrite_late_result(monitor_db, status):
    old = now_shanghai() - timedelta(seconds=181)
    job = _job(monitor_db, status=status, created_at=old, locked_at=old)
    body = directory_sync.sync_status(monitor_db, [job.id], "contacts")
    assert body["done"] and body["jobs"][0]["state"] == "timeout"
    monitor_db.refresh(job)
    assert job.status == status
    job.status = "completed"
    job.result_json = json.dumps({"webridge": {"ok": True, "result": {"items": []}}})
    monitor_db.commit()
    assert directory_sync.sync_status(monitor_db, [job.id], "contacts")["successful"] == 1


def test_failed_and_partial_results_show_counts_and_error(monitor_db):
    first = _job(monitor_db, status="completed", outcome={"ok": True, "result": {"items": [{"account_id": "one"}]}})
    second = _job(monitor_db, status="failed", error_message="webridge_command_timeout")
    body = directory_sync.sync_status(monitor_db, [first.id, second.id], "contacts")
    assert body["done"] and body["successful"] == body["failed"] == 1 and body["count"] == 1
    assert "1 个账号成功、1 个未完成" in body["summary"] and "超时" in body["jobs"][1]["message"]


@pytest.mark.parametrize("change", ["tenant", "other_channel_tenant", "protocol", "deleted", "job_type", "kind", "missing"])
def test_task_identity_boundaries(monitor_db, change):
    job = _job(monitor_db)
    if change == "other_channel_tenant": job.payload_json = json.dumps({"channel_id": 2, "command": {"type": "friends_all"}})
    elif change == "protocol": job.payload_json = json.dumps({"channel_id": 3, "command": {"type": "friends_all"}})
    elif change == "deleted": job.payload_json = json.dumps({"channel_id": 4, "command": {"type": "friends_all"}})
    elif change == "job_type": job.job_type = "other"
    monitor_db.commit()
    with pytest.raises(directory_sync.DirectorySyncError) as error:
        directory_sync.sync_status(monitor_db, [100 if change == "tenant" else job.id + 100 if change == "missing" else job.id], "chatrooms" if change == "kind" else "contacts")
    assert error.value.status_code == 404


@pytest.mark.parametrize("value", ["", "0", "-1", "1,", "1.1", "１", "1" * 30, ",".join("1" for _ in range(201))])
def test_invalid_job_numbers_are_rejected(value):
    with pytest.raises(directory_sync.DirectorySyncError):
        directory_sync.parse_job_ids(value)


def test_job_numbers_keep_input_order_without_duplicates():
    assert directory_sync.parse_job_ids("3,1,3") == [3, 1]


@pytest.mark.parametrize("result,successful", [
    ({}, False),
    ({"status": "unknown"}, False),
    ({"items": []}, True),
    ({"groups": []}, False),
    ({"padding": "x" * 50000}, False),
    ({"items": [], "padding": "x" * 50000}, True),
])
def test_actual_claim_and_return_chain_distinguishes_empty_directory_and_unknown(monitor_db, monkeypatch, result, successful):
    channel = monitor_db.get(Channel, 1)
    identity = {"device_id": "monitor-device", "session_id": "monitor-session", "account_id": "wxid_monitor"}
    channel.runtime_info_json = json.dumps({"webridge": identity})
    monitor_db.commit()
    monkeypatch.setattr(service, "_poll_maintenance", lambda *args: None)
    job = service.enqueue_command(monitor_db, channel, command_name="friends_all", args={})
    monitor_db.commit()
    commands = service.claim_commands(monitor_db, channel, identity)
    assert len(commands) == 1
    service.accept_command_result(monitor_db, channel, {**identity, "ok": True, "request_id": commands[0]["request_id"], "result": result})
    monitor_db.commit()
    status = directory_sync.sync_status(monitor_db, [job.id], "contacts")
    assert status["done"] and bool(status["successful"]) is successful


@pytest.mark.parametrize("path", ["/contacts", "/chatrooms"])
def test_routes_report_task_progress_and_validate_access(sync_context, path):
    client, ids = sync_context
    csrf = _login(client, "operator")
    submitted = client.post(path + "/sync", data={"csrf_token": csrf, "channel_id": ids["sync_channels"][0]}, headers={"X-Requested-With": "fetch"})
    assert submitted.status_code == 200 and submitted.json()["ok"] is True
    task_id = submitted.json()["jobs"][0]["id"]
    status_url = submitted.json()["status_url"]
    assert status_url == f"{path}/sync/status?job_ids={task_id}"
    progress = client.get(status_url)
    assert progress.status_code == 200 and progress.json()["jobs"][0]["state"] == "pending"
    with ids["factory"]() as db:
        job = db.get(TaskJob, task_id)
        job.status = "completed"
        job.result_json = json.dumps({"webridge": {"ok": True, "result": {"items": []}}})
        db.commit()
    done = client.get(status_url)
    assert done.status_code == 200 and done.json()["done"] and done.json()["successful"] == 1
    other_path = "/chatrooms" if path == "/contacts" else "/contacts"
    assert client.get(f"{other_path}/sync/status?job_ids={task_id}").status_code == 404
    assert client.get(path + "/sync/status?job_ids=１").status_code == 400
    assert client.get(path + "/sync/status?job_ids=999999").status_code == 404
    assert client.post(path + "/sync", data={"csrf_token": "bad"}, headers={"X-Requested-With": "fetch"}).status_code == 403
    _login(client, "readonly")
    assert client.get(status_url).status_code == 403
