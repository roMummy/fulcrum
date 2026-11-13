"""好友备注、删除的真实命令契约、结果确认、幂等和租户边界。"""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob, WorkWechatContact
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import friend_actions, friend_check, service
from test_friend_check_service import db  # noqa: F401


def _start(db, action="remark", suffix="- 单删"):
    return friend_actions.start_action(db, db.get(Channel, 1), 1, action, suffix)


def _payload(action="remark", **updates):
    business = {"ok": True, "id": "wxid_friend"}
    if action == "remark":
        business.update(status="applied_locally", remark="好友备注- 单删", local_verified=True, server_confirmed=False)
    business.update(updates)
    return {"ok": True, "command_name": friend_actions.COMMANDS[action], "result": business}


def _finish(db, job, payload, status="completed"):
    job.status = status
    job.result_json = json.dumps({"webridge": payload}, ensure_ascii=False)
    db.commit()
    return friend_actions.poll_action(db, db.get(Channel, 1), job.id)


@pytest.mark.parametrize("display_name,name,expected", [
    ("当前备注", "昵称", "当前备注- 单删"),
    ("昵称", "昵称", "昵称- 单删"),
    (None, "昵称", "昵称- 单删"),
])
def test_remark_appends_suffix_to_current_remark_or_nickname(db, display_name, name, expected):
    contact = db.get(WorkWechatContact, 1)
    contact.display_name, contact.name = display_name, name
    db.commit()
    job = _start(db)
    assert parse_job_payload(job)["command"]["params"] == {"id": "wxid_friend", "remark": expected}
    assert job.related_type == friend_actions.RELATED_TYPE and job.related_id == 1
    assert contact.display_name == display_name
    assert friend_actions.poll_action(db, db.get(Channel, 1), job.id) == {"state": "pending", "result": None}


def test_delete_command_only_has_real_target_id_and_does_not_delete_before_confirmation(db):
    job = _start(db, "delete")
    command = parse_job_payload(job)["command"]
    assert command["type"] == "delete_friend" and command["params"] == {"id": "wxid_friend"}
    assert db.get(WorkWechatContact, 1).deleted_at is None


@pytest.mark.parametrize("suffix", ["", "  ", None, 1, "\n后缀", "\x00", "x" * 256])
def test_invalid_suffix_does_not_enqueue(db, suffix):
    with pytest.raises(friend_check.FriendCheckError) as error:
        _start(db, suffix=suffix)
    assert error.value.status_code == 400
    assert not db.scalars(select(TaskJob)).all()


def test_native_remark_length_validation_is_not_silently_truncated(db):
    suffix = "a" * 32
    job = _start(db, suffix=suffix)
    assert parse_job_payload(job)["command"]["params"]["remark"] == "好友备注" + suffix
    response = _finish(db, job, _payload(ok=False, status="failed", error="备注超过微信长度限制"))
    assert response["result"]["ok"] is False and "长度" in response["result"]["message"]
    assert db.get(WorkWechatContact, 1).display_name == "好友备注"


@pytest.mark.parametrize("first,second", [("check", "remark"), ("remark", "check"), ("remark", "delete"), ("delete", "remark")])
def test_checks_and_actions_share_one_channel_lock(db, first, second):
    def start(action):
        return friend_check.start_check(db, db.get(Channel, 1), 1) if action == "check" else _start(db, action)
    first_job = start(first)
    with pytest.raises(friend_check.FriendCheckError) as error:
        start(second)
    assert error.value.status_code == 409
    assert len(db.scalars(select(TaskJob)).all()) == 1
    first_job.status = "failed"
    db.commit()
    assert start(second).id != first_job.id


@pytest.mark.parametrize("contact_id", [2, 3, 4, 5, 999])
def test_start_rejects_foreign_other_channel_deleted_and_nonfriend_contacts(db, contact_id):
    with pytest.raises(friend_check.FriendCheckError) as error:
        friend_actions.start_action(db, db.get(Channel, 1), contact_id, "delete")
    assert error.value.status_code == 404


def test_channel_and_action_validation(db):
    channel = db.get(Channel, 1)
    channel.is_enabled = False
    db.commit()
    with pytest.raises(friend_check.FriendCheckError) as error:
        _start(db)
    assert error.value.status_code == 400
    channel.is_enabled = True
    with pytest.raises(friend_check.FriendCheckError):
        _start(db, "not-supported")
    for channel_id in (3, 4):
        with pytest.raises(friend_check.FriendCheckError) as error:
            friend_actions.start_action(db, db.get(Channel, channel_id), 1, "delete")
        assert error.value.status_code == 404
    with tenant_scope(2), pytest.raises(friend_check.FriendCheckError):
        friend_actions.start_action(db, channel, 1, "delete")


@pytest.mark.parametrize("status", ["applied_locally", "unchanged"])
def test_confirmed_remark_updates_local_contact_once(db, status):
    job = _start(db)
    response = _finish(db, job, _payload(status=status))
    assert response["result"]["ok"] is True
    assert response["result"]["action"] == "remark"
    assert response["result"]["contact"]["remark"] == "好友备注- 单删"
    contact = db.get(WorkWechatContact, 1)
    contact.display_name = "后续人工修改"
    db.commit()
    again = friend_actions.poll_action(db, db.get(Channel, 1), job.id)
    assert again["result"]["ok"] is True
    assert contact.display_name == "后续人工修改"


@pytest.mark.parametrize("updates", [
    {"status": "unknown"}, {"status": "failed"}, {"status": "accepted"},
    {"ok": False}, {"ok": "true"}, {"id": "wrong"}, {"id": None},
    {"remark": None}, {"remark": ""}, {"remark": "x" * 256},
    {"error": "微信返回失败"},
])
def test_unconfirmed_remark_never_changes_local_contact(db, updates):
    response = _finish(db, _start(db), _payload(**updates))
    assert response["result"]["ok"] is False
    assert db.get(WorkWechatContact, 1).display_name == "好友备注"


def test_confirmed_delete_soft_deletes_contact_and_repeated_poll_is_safe(db):
    job = _start(db, "delete")
    response = _finish(db, job, _payload("delete"))
    contact = db.get(WorkWechatContact, 1)
    assert response["result"]["ok"] is True and response["result"]["action"] == "delete"
    assert contact.deleted_at is not None and contact.sync_status == "deleted"
    when = contact.deleted_at
    assert friend_actions.poll_action(db, db.get(Channel, 1), job.id)["result"]["ok"] is True
    assert contact.deleted_at == when
    with pytest.raises(friend_check.FriendCheckError) as error:
        _start(db, "delete")
    assert error.value.status_code == 404


@pytest.mark.parametrize("updates", [{"ok": False}, {"ok": "true"}, {"id": "other"}, {"status": "unknown"}, {"error": "没有权限"}])
def test_unknown_or_failed_delete_does_not_delete_local_contact(db, updates):
    response = _finish(db, _start(db, "delete"), _payload("delete", **updates))
    assert response["result"]["ok"] is False
    assert db.get(WorkWechatContact, 1).deleted_at is None


def test_long_poll_delete_accepts_documented_outer_success_with_only_target_in_result(db):
    payload = _payload("delete")
    payload["result"].pop("ok")
    response = _finish(db, _start(db, "delete"), payload)
    assert response["result"]["ok"] is True
    assert db.get(WorkWechatContact, 1).deleted_at is not None


@pytest.mark.parametrize("location", ["failed_job", "outer", "business"])
def test_missing_phone_service_has_clear_chinese_error_and_does_not_delete_contact(db, location):
    job = _start(db, "delete")
    payload = _payload("delete")
    status = "completed"
    if location == "failed_job":
        status = "failed"
        job.error_message = "service class not found"
    elif location == "outer":
        payload.update(ok=False, error="service class not found")
    else:
        payload["result"].update(ok=False, error="service class not found")
    response = _finish(db, job, payload, status=status)
    assert response["result"]["ok"] is False
    assert response["result"]["message"] == "手机端微桥无法找到微信的删除服务（service class not found），删除未获成功确认。"
    assert db.get(WorkWechatContact, 1).deleted_at is None


@pytest.mark.parametrize("outer", [
    {"ok": False}, {"ok": "true"}, {"command_name": "other_command"}, {"error": "插件报错"},
])
def test_outer_envelope_must_confirm_same_command(db, outer):
    payload = {**_payload("delete"), **outer}
    assert _finish(db, _start(db, "delete"), payload)["result"]["ok"] is False
    assert db.get(WorkWechatContact, 1).deleted_at is None


def test_legacy_http_result_wrapper_is_checked(db):
    response = _finish(db, _start(db), {"ok": True, "result": {"ok": True, "result": _payload()["result"]}})
    assert response["result"]["ok"] is True


def test_poll_rejects_other_channel_or_tampered_target_and_wrong_related_type(db):
    job = _start(db)
    with pytest.raises(friend_check.FriendCheckError) as error:
        friend_actions.poll_action(db, db.get(Channel, 2), job.id)
    assert error.value.status_code == 404
    data = parse_job_payload(job)
    data["command"]["params"]["id"] = "wrong"
    job.payload_json = json.dumps(data)
    db.commit()
    with pytest.raises(friend_check.FriendCheckError):
        friend_actions.poll_action(db, db.get(Channel, 1), job.id)
    job.related_type = "webridge_friend_check"
    db.commit()
    with pytest.raises(friend_check.FriendCheckError):
        friend_actions.poll_action(db, db.get(Channel, 1), job.id)


@pytest.mark.parametrize("state", ["pending", "running"])
def test_timeout_cancels_unclaimed_action_or_marks_uncertain_running_result(db, state):
    job = _start(db, "delete")
    job.status = state
    job.created_at = now_shanghai() - timedelta(seconds=70)
    if state == "running":
        job.locked_at = job.created_at
    db.commit()
    result = friend_actions.poll_action(db, db.get(Channel, 1), job.id)["result"]
    assert result["ok"] is False and job.status == "failed"
    assert ("未执行" if state == "pending" else "未确认") in result["message"]
    assert db.get(WorkWechatContact, 1).deleted_at is None
    assert friend_check.start_check(db, db.get(Channel, 1), 1).id != job.id


def _prepare_phone(db, monkeypatch):
    identity = {"device_id": "test-device", "session_id": "test-session", "account_id": "wxid_logged_in"}
    channel = db.get(Channel, 1)
    channel.runtime_info_json = json.dumps({"webridge": identity})
    db.commit()
    monkeypatch.setattr(service, "_poll_maintenance", lambda *args: None)
    return channel, identity


def test_expired_delete_candidate_cannot_be_revived_when_phone_claims_it(db, monkeypatch):
    job = _start(db, "delete")
    job.created_at = now_shanghai() - timedelta(seconds=70)
    db.commit()
    channel, identity = _prepare_phone(db, monkeypatch)
    parse = service.parse_job_payload
    outcomes = []
    def expire_after_select(candidate):
        data = parse(candidate)
        if candidate.id == job.id and not outcomes:
            with tenant_session(db.info["test_factory"]) as other:
                outcomes.append(friend_actions.poll_action(other, other.get(Channel, 1), job.id))
        return data
    monkeypatch.setattr(service, "parse_job_payload", expire_after_select)
    assert service.claim_commands(db, channel, identity) == []
    assert outcomes[0]["result"]["ok"] is False
    db.refresh(job)
    assert job.status == "failed" and job.attempts == 0


def test_timed_out_running_delete_ignores_late_success_result(db, monkeypatch):
    job = _start(db, "delete")
    channel, identity = _prepare_phone(db, monkeypatch)
    assert len(service.claim_commands(db, channel, identity)) == 1
    job.locked_at = now_shanghai() - timedelta(seconds=70)
    db.commit()
    assert friend_actions.poll_action(db, channel, job.id)["result"]["ok"] is False
    late = service.accept_command_result(db, channel, {**identity, "request_id": f"job_{job.id}", **_payload("delete")})
    db.commit()
    assert late["deduplicated"] is True and late["status"] == "failed"
    assert db.get(WorkWechatContact, 1).deleted_at is None


@pytest.mark.parametrize("action", ["remark", "delete"])
def test_real_claim_result_and_poll_flow_confirms_local_update(db, monkeypatch, action):
    job = _start(db, action)
    channel, identity = _prepare_phone(db, monkeypatch)
    claimed = service.claim_commands(db, channel, identity)
    assert len(claimed) == 1 and claimed[0]["command_name"] == friend_actions.COMMANDS[action]
    assert service.claim_commands(db, channel, identity) == []
    result = service.accept_command_result(db, channel, {
        **identity, "request_id": claimed[0]["request_id"], **_payload(action),
    })
    db.commit()
    assert result["status"] == "completed"
    # 不依赖浏览器继续轮询，手机回报本身就完成本地资料更新。
    contact = db.get(WorkWechatContact, 1)
    assert (contact.deleted_at is not None) if action == "delete" else contact.display_name == "好友备注- 单删"
    response = friend_actions.poll_action(db, channel, job.id)
    assert response["result"]["ok"] is True
    contact = db.get(WorkWechatContact, 1)
    assert (contact.deleted_at is not None) if action == "delete" else contact.display_name == "好友备注- 单删"


def test_competing_phone_claims_cannot_execute_delete_twice(db, monkeypatch):
    job = _start(db, "delete")
    channel, identity = _prepare_phone(db, monkeypatch)
    parse = service.parse_job_payload
    second_commands = []
    competing = False
    def claim_after_select(candidate):
        nonlocal competing
        data = parse(candidate)
        if candidate.id == job.id and not competing:
            competing = True
            with tenant_session(db.info["test_factory"]) as other:
                second_commands.extend(service.claim_commands(other, other.get(Channel, 1), identity))
        return data
    monkeypatch.setattr(service, "parse_job_payload", claim_after_select)
    assert service.claim_commands(db, channel, identity) == []
    assert len(second_commands) == 1
    db.refresh(job)
    assert job.status == "running" and job.attempts == 1


@pytest.mark.parametrize("raw", ["null", "[]", "not-json", "{}"])
def test_unreadable_delete_result_never_soft_deletes_contact(db, raw):
    job = _start(db, "delete")
    job.status, job.result_json = "completed", raw
    db.commit()
    assert friend_actions.poll_action(db, db.get(Channel, 1), job.id)["result"]["ok"] is False
    assert db.get(WorkWechatContact, 1).deleted_at is None
