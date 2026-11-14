"""真实账号路由的操作反馈：提交只代表排队，成功必须等设备的有效回报。"""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

import app.main as main_module
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, TaskJob
from app.services.wechat_webridge.contract import JOB_TYPE
from test_provider_node_pages import _login, provider_page_context  # noqa: F401
from test_switch_autosave import webridge_group  # noqa: F401


def _headers(csrf: str = "") -> dict[str, str]:
    headers = {"X-Requested-With": "fetch"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    return headers


def _submit_members_sync(client, channel_id: int, room_id: int, csrf: str):
    response = client.post(
        f"/channels/{channel_id}/chatrooms/{room_id}/sync", headers=_headers(csrf),
    )
    assert response.status_code == 200
    queued = response.json()
    assert queued["ok"] is True and queued["done"] is False
    assert queued["successful"] == queued["failed"] == 0
    # 单群同步同时读取成员和公告，只有两份设备回报均结束后才算整个操作结束。
    assert len(queued["jobs"]) == 2 and all(row["state"] == "pending" for row in queued["jobs"])
    assert queued["status_url"].startswith(f"/channels/{channel_id}/operations/status?job_ids=")
    return queued


def _finish_job(ids, job_id: int, *, status: str = "completed", successful: bool = True) -> None:
    with ids["factory"]() as db:
        job = db.get(TaskJob, job_id)
        command = json.loads(job.payload_json)["command"]
        # accept_command_result 的真实落库格式保留回报信封，目录列表在 result 内。
        detail = {"ok": successful, "members": [{"account_id": "member-a"}, {"account_id": "member-b"}]}
        if command["type"] == "get_group_notice":
            detail = {"room_id": command["params"]["room_id"], "cached_notice_found": False, "html_cached": False, "summary": None, "html": None}
        job.result_json = json.dumps({"webridge": {
            "request_id": command["request_id"], "ok": successful,
            "result": detail,
        }})
        job.status = status
        job.finished_at = now_shanghai()
        db.commit()


def test_members_sync_waits_for_a_real_device_result(webridge_group) -> None:
    client, ids, channel_id, room_id = webridge_group
    queued = _submit_members_sync(client, channel_id, room_id, _login(client, "admin"))
    pending = client.get(queued["status_url"])
    assert pending.status_code == 200 and pending.json()["done"] is False
    assert pending.headers["cache-control"] == "no-store"
    with ids["factory"]() as db:
        job = db.get(TaskJob, queued["jobs"][0]["id"])
        payload = json.loads(job.payload_json)
        assert payload["channel_id"] == channel_id
        assert payload["command"]["type"] == "room_members"
        assert payload["command"]["params"] == {"room_id": "1@chatroom"}
        assert job.status == "pending" and job.result_json is None

    _finish_job(ids, queued["jobs"][0]["id"])
    partial = client.get(queued["status_url"]).json()
    assert partial["done"] is False and partial["successful"] == 1
    _finish_job(ids, queued["jobs"][1]["id"])
    completed = client.get(queued["status_url"]).json()
    assert completed["done"] is True and completed["successful"] == 2 and completed["failed"] == 0
    assert completed["jobs"][0]["state"] == "completed"
    assert "群成员同步完成" in completed["jobs"][0]["message"] and "2 位成员" in completed["jobs"][0]["message"]


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_members_sync_reports_failure_even_if_the_queue_marks_it_completed(webridge_group, status) -> None:
    client, ids, channel_id, room_id = webridge_group
    queued = _submit_members_sync(client, channel_id, room_id, _login(client, "admin"))
    _finish_job(ids, queued["jobs"][0]["id"], status=status, successful=False)
    _finish_job(ids, queued["jobs"][1]["id"])
    failed = client.get(queued["status_url"]).json()
    assert failed["done"] is True and failed["successful"] == 1 and failed["failed"] == 1
    assert failed["jobs"][0]["state"] == "failed"
    assert "未完成" in failed["summary"] or "失败" in failed["summary"]


def test_status_hides_jobs_from_other_channels_and_tenants(webridge_group) -> None:
    client, ids, channel_id, room_id = webridge_group
    queued = _submit_members_sync(client, channel_id, room_id, _login(client, "admin"))
    with ids["factory"]() as db:
        own_channel = Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="另一微桥", channel_type="wechat_webridge", is_enabled=True)
        beta_bot = Bot(tenant_id=ids["beta"], name="乙机器人")
        db.add_all([own_channel, beta_bot])
        db.flush()
        beta_channel = Channel(tenant_id=ids["beta"], bot_id=beta_bot.id, name="乙微桥", channel_type="wechat_webridge", is_enabled=True)
        db.add(beta_channel)
        db.flush()
        other_channel_job = TaskJob(
            tenant_id=ids["alpha"], job_type=JOB_TYPE, status="pending",
            payload_json=json.dumps({"channel_id": own_channel.id, "command": {"type": "room_members"}}),
        )
        # 即使任务载荷声称属于本账号，租户字段不匹配也必须拒绝。
        other_tenant_job = TaskJob(
            tenant_id=ids["beta"], job_type=JOB_TYPE, status="pending",
            payload_json=json.dumps({"channel_id": channel_id, "command": {"type": "room_members"}}),
        )
        db.add_all([other_channel_job, other_tenant_job])
        db.commit()
        hidden_ids = [other_channel_job.id, other_tenant_job.id]
        beta_channel_id = beta_channel.id

    for hidden_id in hidden_ids:
        assert client.get(f"/channels/{channel_id}/operations/status?job_ids={hidden_id}").status_code == 404
        assert client.get(f"{queued['status_url']},{hidden_id}").status_code == 404
    assert client.get(f"/channels/{beta_channel_id}/operations/status?job_ids={queued['jobs'][0]['id']}").status_code == 404


@pytest.mark.parametrize("suffix", ["", "0", "-1", "1,,2", "abc", "1" * 19])
def test_status_rejects_invalid_task_ids(webridge_group, suffix) -> None:
    client, _ids, channel_id, _room_id = webridge_group
    _login(client, "admin")
    response = client.get(f"/channels/{channel_id}/operations/status?job_ids={suffix}")
    assert response.status_code == 400


def test_operation_routes_require_csrf_and_the_operator_role(webridge_group) -> None:
    client, _ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    paths = [
        (f"/channels/{channel_id}/chatrooms/{room_id}/sync", {}),
        (f"/channels/{channel_id}/wechat-webridge/command", {"command_name": "room_members", "args_json": '{"room_id":"1@chatroom"}'}),
        (f"/channels/{channel_id}/health-check", {}),
    ]
    for path, data in paths:
        assert client.post(path, data=data, headers=_headers()).status_code == 403
        assert client.post(path, data=data, headers=_headers("invalid")).status_code == 403

    queued = _submit_members_sync(client, channel_id, room_id, csrf)
    csrf = _login(client, "readonly")
    for path, data in paths:
        assert client.post(path, data={**data, "csrf_token": csrf}, headers=_headers(csrf)).status_code == 403
    assert client.get(queued["status_url"]).status_code == 403


def test_normal_form_redirect_preserves_the_pending_operation(webridge_group) -> None:
    client, _ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    response = client.post(
        f"/channels/{channel_id}/chatrooms/{room_id}/sync", data={"csrf_token": csrf},
    )
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    query = parse_qs(target.query)
    assert target.path == f"/channels/{channel_id}/chatrooms/{room_id}"
    assert query["operation_channel"] == [str(channel_id)]
    assert len(query["operation_jobs"][0].split(",")) == 2
    assert all(part.isdigit() for part in query["operation_jobs"][0].split(","))
    status = client.get(f"/channels/{channel_id}/operations/status?job_ids={query['operation_jobs'][0]}").json()
    assert status["done"] is False and status["jobs"][0]["state"] == "pending"


def test_dashboard_command_returns_pending_operation_feedback(webridge_group) -> None:
    client, _ids, channel_id, _room_id = webridge_group
    csrf = _login(client, "admin")
    response = client.post(
        f"/channels/{channel_id}/wechat-webridge/command",
        data={"command_name": "room_members", "args_json": '{"room_id":"1@chatroom"}', "csrf_token": csrf},
        headers=_headers(csrf),
    )
    assert response.status_code == 200
    queued = response.json()
    assert queued["ok"] is True and queued["done"] is False
    assert queued["jobs"][0]["state"] == "pending"
    assert client.get(queued["status_url"]).json()["done"] is False


@pytest.mark.parametrize("command,args", [("room_members", "broken"), ("room_members", "[]"), ("/invalid", "{}")])
def test_dashboard_command_returns_json_errors_for_invalid_parameters(webridge_group, command, args) -> None:
    client, _ids, channel_id, _room_id = webridge_group
    csrf = _login(client, "admin")
    response = client.post(
        f"/channels/{channel_id}/wechat-webridge/command",
        data={"command_name": command, "args_json": args, "csrf_token": csrf}, headers=_headers(csrf),
    )
    assert response.status_code in {200, 400}
    result = response.json()
    assert result["ok"] is False and result["done"] is True and result["failed"] == 1
    assert result["summary"]


def test_health_check_reports_stale_phone_as_offline_in_json(webridge_group) -> None:
    client, ids, channel_id, _room_id = webridge_group
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        channel.runtime_info_json = json.dumps({"webridge": {
            "online": True, "last_seen_at": (now_shanghai() - timedelta(minutes=10)).isoformat(),
        }})
        db.commit()
    response = client.post(f"/channels/{channel_id}/health-check", headers=_headers(csrf))
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False and result["done"] is True and result["failed"] == 1
    assert "最近一次状态已登记" not in result["summary"]
    assert "离线" in result["summary"] or "未收到" in result["summary"]


def test_cloud_group_sync_tracks_both_jobs_and_reports_a_mixed_result(webridge_group, monkeypatch) -> None:
    client, ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        channel = db.get(Channel, channel_id)
        channel.channel_type = "workwechat"
        channel.adapter_config_json = json.dumps({"workwechat_delivery_mode": "cloud_poll"})
        db.commit()
    monkeypatch.setattr(main_module, "notify_cloud_job_enqueued", lambda _channel, _job: None)
    response = client.post(f"/channels/{channel_id}/chatrooms/{room_id}/sync", headers=_headers(csrf))
    assert response.status_code == 200
    queued = response.json()
    assert queued["ok"] is True and queued["done"] is False and len(queued["jobs"]) == 2
    with ids["factory"]() as db:
        jobs = [db.get(TaskJob, row["id"]) for row in queued["jobs"]]
        payloads = [json.loads(job.payload_json) for job in jobs]
        assert {payload["action"] for payload in payloads} == {"sync_chatrooms", "sync_chatroom_members"}
        for job, payload in zip(jobs, payloads):
            successful = payload["action"] == "sync_chatrooms"
            job.status = "completed"
            job.finished_at = now_shanghai()
            # 企微控制任务即使落成 completed，provider 显式失败也不能报全成功。
            job.result_json = json.dumps({
                "action": payload["action"], "adapter_result": {"ok": successful},
                "total": 1, "synced_members": 0,
            })
        db.commit()
    completed = client.get(queued["status_url"]).json()
    assert completed["done"] is True and completed["successful"] == completed["failed"] == 1
    assert {row["state"] for row in completed["jobs"]} == {"completed", "failed"}
    assert "未完成" in completed["summary"]


def test_ipad_failed_group_targets_return_json_failure(webridge_group, monkeypatch) -> None:
    client, ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        db.get(Channel, channel_id).channel_type = "wechat_ipad"
        db.commit()
    monkeypatch.setattr(main_module, "sync_wechat_ipad_chatrooms", lambda _db, _channel, **_kwargs: {
        "last_status_summary": "群资料同步未完成",
        "runtime_info": {"last_chatroom_sync_failed_targets": ["1@chatroom"]},
    })
    for path in (f"/channels/{channel_id}/wechat-ipad/sync-chatrooms", f"/channels/{channel_id}/chatrooms/{room_id}/sync"):
        response = client.post(path, headers=_headers(csrf))
        assert response.status_code == 200
        result = response.json()
        assert result["ok"] is False and result["done"] is True
        assert result["successful"] == 0 and result["failed"] == 1 and result["refresh"] is False


def test_contact_sync_preserves_the_provider_failure_in_json(webridge_group, monkeypatch) -> None:
    import app.services.workwechat_contacts as contacts_service

    client, ids, channel_id, _room_id = webridge_group
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        db.get(Channel, channel_id).channel_type = "workwechat"
        db.commit()
    monkeypatch.setattr(contacts_service, "_fetch_provider_contacts", lambda _channel, **_kwargs: {
        "ok": False, "error": "客户端暂时不可用",
    })
    response = client.post(f"/channels/{channel_id}/workwechat/sync-contacts", headers=_headers(csrf))
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False and result["done"] is True and result["failed"] == 1
    assert result["successful"] == 0 and "失败" in result["summary"]


def test_explicit_danger_tone_and_incomplete_notices_render_as_errors(provider_page_context) -> None:
    client, _ids = provider_page_context
    _login(client, "admin")
    explicit = client.get("/channels", params={"notice": "同步完成", "tone": "danger"})
    assert explicit.status_code == 200 and explicit.context["notice_tone"] == "danger"
    assert 'data-action-monitor data-state="error"' in explicit.text
    incomplete = client.get("/channels", params={"notice": "群资料同步未完成"})
    assert incomplete.status_code == 200 and incomplete.context["notice_tone"] == "danger"
    assert 'data-action-monitor data-state="error"' in incomplete.text


def test_plain_form_posts_are_csrf_checked_on_every_state_changing_route(webridge_group) -> None:
    """没有页面脚本时的普通表单提交也必须校验 CSRF。

    此前这些路由只对带 X-Requested-With: fetch 的请求校验,普通表单 POST 完全不校验,
    第三方页面可以诱导已登录的运维人员发起同步、健康检查等操作。
    """

    client, _ids, channel_id, room_id = webridge_group
    csrf = _login(client, "admin")
    paths = [
        "/bots/1/status-check",
        f"/channels/{channel_id}/health-check",
        f"/channels/{channel_id}/wechat-ipad/pull-messages",
        f"/channels/{channel_id}/wechat-ipad/sync-chatrooms",
        f"/channels/{channel_id}/wechat-ipad/sync-contacts",
        f"/channels/{channel_id}/workwechat/sync-contacts",
        f"/channels/{channel_id}/workwechat/sync-chatrooms",
        f"/channels/{channel_id}/wechat-winauto/sync-chatrooms",
        f"/channels/{channel_id}/chatrooms/{room_id}/sync",
        "/plugins/refresh",
    ]
    for path in paths:
        assert client.post(path, follow_redirects=False).status_code == 403, path                       # 没有令牌
        assert client.post(path, data={"csrf_token": "invalid"}, follow_redirects=False).status_code == 403, path
        # 令牌正确时不再被 CSRF 拦下;后面可能因渠道类型不符等返回别的状态,这里只关心不是 403
        assert client.post(path, data={"csrf_token": csrf}, follow_redirects=False).status_code != 403, path
