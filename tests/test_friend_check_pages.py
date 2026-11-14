"""通过真实租户登录与命令回报验证好友检测页面和接口。"""

import json
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="friend-check-page-logs-"))

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob, WorkWechatContact
from app.services.wechat_webridge import service
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


IDENTITY = {"device_id": "friend-check-device", "session_id": "friend-check-session", "account_id": "wxid_check_owner"}


@pytest.fixture()
def friend_check_context(provider_page_context, monkeypatch):
    client, ids = provider_page_context
    monkeypatch.setattr(service, "_poll_maintenance", lambda *_: None)
    with ids["factory"]() as db:
        channels = [
            Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="检测账号", channel_type="wechat_webridge", is_enabled=True,
                    runtime_info_json=json.dumps({"webridge": IDENTITY})),
            Channel(tenant_id=ids["alpha"], bot_id=ids["bot"], name="第二检测账号", channel_type="wechat_webridge", is_enabled=True),
            Channel(tenant_id=ids["beta"], bot_id=ids["bot"], name="其他企业微桥", channel_type="wechat_webridge", is_enabled=True),
        ]
        db.add_all(channels)
        db.flush()
        contacts = [WorkWechatContact(
            tenant_id=ids["alpha"], channel_id=channels[0].id, contact_kind="friend",
            contact_external_id=f"wxid_check_{index}", name=f"检测好友{index}", display_name=f"备注{index}",
            raw_contact_json='{"pay_name_status":"ok","pay_name_last_char":"旧"}',
        ) for index in range(3)]
        db.add_all(contacts)
        db.add(WorkWechatContact(tenant_id=ids["beta"], channel_id=channels[2].id, contact_kind="friend", contact_external_id="wxid_other_tenant", name="其他企业好友"))
        db.flush()
        ids = {**ids, "bridge_channel": channels[0].id, "other_bridge_channel": channels[1].id,
               "beta_bridge_channel": channels[2].id, "bridge_contacts": [contact.id for contact in contacts]}
        db.commit()
    return client, ids


def _state(client, ids, channel_id=None):
    """页面与测试统一先读取持久记录，所有后续操作携带当前代次。"""
    return client.get(f"/contacts/check/{channel_id or ids['bridge_channel']}/state").json()


def _start(client, ids, csrf, contact_id=None):
    state = _state(client, ids)
    target = contact_id or ids["bridge_contacts"][0]
    # 新检测先确认本轮间隔；已有结果只能通过复查入口再次查询，不能重置已完成进度。
    response = client.post(
        f"/contacts/check/{ids['bridge_channel']}/run", headers={"X-CSRF-Token": csrf},
        json={"generation": state["generation"], "interval_seconds": 1},
    )
    if response.status_code != 200:
        return response
    scan = not any(item["contact"]["id"] == target for item in state["results"])
    return client.post(
        f"/contacts/check/{ids['bridge_channel']}/friends/{target}",
        headers={"X-CSRF-Token": csrf}, json={"generation": state["generation"], "scan": scan},
    )


def _poll(client, ids, csrf, job_id, channel_id=None):
    return client.post(
        f"/contacts/check/{channel_id or ids['bridge_channel']}/jobs/{job_id}/poll",
        headers={"X-CSRF-Token": csrf}, json={"generation": _state(client, ids, channel_id).get("generation", "unavailable")},
    )


def _phone_result(ids, *, ok=True, status="ok", last_char="明", target="wxid_check_0"):
    # 使用真实命令领取与回报处理函数，模拟手机而不触碰实际设备。
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        channel = db.get(Channel, ids["bridge_channel"])
        commands = service.claim_commands(db, channel, IDENTITY)
        assert len(commands) == 1
        command = commands[0]
        assert command["command_name"] == "friend_pay_name"
        assert command["args"] == {"id": "wxid_check_0", "refresh": True}
        service.accept_command_result(db, channel, {
            **IDENTITY, "request_id": command["request_id"], "command_name": "friend_pay_name", "ok": ok,
            "result": {"account_id": target, "pay_name_status": status, "pay_name_last_char": last_char},
            "error": "" if ok else "模拟插件查询失败",
        })


def test_page_starts_with_empty_tables_and_keeps_selected_account(friend_check_context):
    client, ids = friend_check_context
    _login(client, "operator")
    directory = client.get(f"/contacts?channel_id={ids['bridge_channel']}")
    assert directory.status_code == 200
    assert f'/contacts/check?channel_id={ids["bridge_channel"]}' in directory.text
    response = client.get(f"/contacts/check?channel_id={ids['bridge_channel']}")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert "互相是好友" in response.text and "推测" in response.text
    assert "contact-check-caveat" not in response.text and "共同群" not in response.text
    assert "请保持本页打开" not in response.text and "每次重新查询" not in response.text
    assert "检测好友0" not in response.text
    for category in ("friend", "deleted", "unknown"):
        assert f'<tbody data-result-body="{category}"></tbody>' in response.text
    assert "其他企业微桥" not in response.text


def test_all_friends_are_loaded_without_pagination_or_other_contact_kinds(friend_check_context):
    client, ids = friend_check_context
    _login(client, "operator")
    with ids["factory"]() as db:
        db.add_all([WorkWechatContact(tenant_id=ids["alpha"], channel_id=ids["bridge_channel"], contact_kind="friend", contact_external_id=f"wxid_full_{i}", name=f"全量好友{i}") for i in range(350)])
        excluded = [
            WorkWechatContact(tenant_id=ids["alpha"], channel_id=ids["bridge_channel"], contact_kind="group", contact_external_id="test@chatroom"),
            WorkWechatContact(tenant_id=ids["alpha"], channel_id=ids["bridge_channel"], contact_kind="friend", contact_external_id="wxid_deleted_contact", deleted_at=now_shanghai()),
            WorkWechatContact(tenant_id=ids["alpha"], channel_id=ids["other_bridge_channel"], contact_kind="friend", contact_external_id="wxid_other_channel"),
        ]
        db.add_all(excluded)
        db.commit()
    response = client.get(f"/contacts/check/{ids['bridge_channel']}/friends")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()
    assert result["total"] == len(result["contacts"]) == 353
    assert not {"test@chatroom", "wxid_deleted_contact", "wxid_other_channel", "wxid_other_tenant"}.intersection(contact["external_id"] for contact in result["contacts"])
    assert "pay_name_status" not in response.text


@pytest.mark.parametrize("ok,status,last_char,category", [
    (True, "ok", "明", "friend"), (True, "unavailable", "", "deleted"),
    (False, "failed", "", "unknown"), (True, "querying", "", "unknown"),
])
def test_actual_command_flow_groups_fresh_results(friend_check_context, ok, status, last_char, category):
    client, ids = friend_check_context
    csrf = _login(client, "operator")
    response = _start(client, ids, csrf)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    pending = _poll(client, ids, csrf, job_id).json()
    assert pending["state"] == "pending" and pending["result"] is None
    assert pending["progress"]["cursor"] == 0
    _phone_result(ids, ok=ok, status=status, last_char=last_char)
    result = _poll(client, ids, csrf, job_id).json()
    assert result["state"] == "completed" and result["result"]["category"] == category
    assert result["result"]["contact"]["id"] == ids["bridge_contacts"][0]
    # 完成后再次检测仍创建新命令；联系人中旧的姓名缓存不参与分类或被改写。
    again = _start(client, ids, csrf)
    assert again.status_code == 200 and again.json()["job_id"] != job_id
    with ids["factory"]() as db:
        assert db.get(WorkWechatContact, ids["bridge_contacts"][0]).raw_contact_json == '{"pay_name_status":"ok","pay_name_last_char":"旧"}'


def test_access_csrf_and_channel_boundaries(friend_check_context):
    client, ids = friend_check_context
    csrf = _login(client, "operator")
    assert _start(client, ids, "forged").status_code == 403
    assert client.get(f"/contacts/check/{ids['beta_bridge_channel']}/friends").status_code == 404
    assert client.get(f"/contacts/check/{ids['channel_id']}/friends").status_code == 404
    assert client.post(f"/contacts/check/{ids['other_bridge_channel']}/friends/{ids['bridge_contacts'][0]}", headers={"X-CSRF-Token": csrf},
                       json={"generation": _state(client, ids, ids["other_bridge_channel"])["generation"], "scan": True}).status_code == 404
    response = _start(client, ids, csrf)
    job_id = response.json()["job_id"]
    assert _poll(client, ids, "forged", job_id).status_code == 403
    assert _poll(client, ids, csrf, job_id, ids["other_bridge_channel"]).status_code == 404
    assert _poll(client, ids, csrf, job_id, ids["beta_bridge_channel"]).status_code == 404
    assert _start(client, ids, csrf, ids["bridge_contacts"][1]).status_code == 409
    readonly_csrf = _login(client, "readonly")
    assert client.get("/contacts/check").status_code == 200
    assert _start(client, ids, readonly_csrf).status_code == 403
    assert _poll(client, ids, readonly_csrf, job_id).status_code == 403
    assert "/contacts/check?" in client.get("/contacts").text  # 只读角色可查看已保存记录。


def test_timeout_stops_queued_command_from_later_delivery(friend_check_context):
    client, ids = friend_check_context
    csrf = _login(client, "operator")
    job_id = _start(client, ids, csrf).json()["job_id"]
    with ids["factory"]() as db:
        db.get(TaskJob, job_id).created_at = now_shanghai() - timedelta(seconds=61)
        db.commit()
    result = _poll(client, ids, csrf, job_id).json()["result"]
    assert result["category"] == "unknown" and "超时" in result["reason"]
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        assert service.claim_commands(db, db.get(Channel, ids["bridge_channel"]), IDENTITY) == []
        assert len(db.scalars(select(TaskJob)).all()) == 1
