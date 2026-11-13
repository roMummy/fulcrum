"""好友检测的强制刷新、结果分类、串行与租户边界。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="friend-check-test-logs-"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, TaskJob, WorkWechatContact
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import friend_check
from app.services.wechat_webridge.contract import JOB_TYPE


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'friend-check.db'}")
    for table in (Channel.__table__, WorkWechatContact.__table__, TaskJob.__table__):
        table.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as seed:
        seed.add_all([
            Channel(id=1, tenant_id=1, bot_id=1, name="微桥一号", channel_type="wechat_webridge"),
            Channel(id=2, tenant_id=1, bot_id=1, name="微桥二号", channel_type="wechat_webridge"),
            Channel(id=3, tenant_id=1, bot_id=1, name="其他协议", channel_type="wechat_ipad"),
            Channel(id=4, tenant_id=1, bot_id=1, name="已删账号", channel_type="wechat_webridge", deleted_at=now_shanghai()),
            Channel(id=5, tenant_id=2, bot_id=2, name="另一租户", channel_type="wechat_webridge"),
            WorkWechatContact(id=1, tenant_id=1, channel_id=1, contact_kind="friend", contact_external_id="wxid_friend", name="小明", display_name="好友备注", avatar_url="https://example.test/avatar.png", raw_contact_json='{"pay_name_status":"ok","pay_name_last_char":"旧"}'),
            WorkWechatContact(id=2, tenant_id=1, channel_id=2, contact_kind="friend", contact_external_id="wxid_other", name="小李"),
            WorkWechatContact(id=3, tenant_id=2, channel_id=5, contact_kind="friend", contact_external_id="wxid_private", name="不应显示"),
            WorkWechatContact(id=4, tenant_id=1, channel_id=1, contact_kind="friend", contact_external_id="wxid_deleted", deleted_at=now_shanghai()),
            WorkWechatContact(id=5, tenant_id=1, channel_id=1, contact_kind="group", contact_external_id="room@chatroom"),
        ])
        seed.commit()
    monkeypatch.setattr(friend_check.wakeup, "notify", lambda channel_id: None)
    with tenant_scope(1), tenant_session(factory) as session:
        session.info["test_factory"] = factory
        yield session
    engine.dispose()


def _start(db):
    return friend_check.start_check(db, db.get(Channel, 1), 1)


def _finish(db, job, payload, *, status="completed"):
    job.status = status
    job.result_json = json.dumps({"webridge": payload}, ensure_ascii=False)
    db.commit()
    return friend_check.poll_check(db, db.get(Channel, 1), job.id)


def _payload(status="ok", last_char="明", **updates):
    payload = {"ok": True, "account_id": "wxid_logged_in", "result": {
        "account_id": "wxid_friend", "pay_name_status": status, "pay_name_last_char": last_char,
    }}
    payload.update(updates)
    return payload


def test_lists_only_current_tenant_webridge_friends_without_paging(db):
    for i in range(40):
        db.add(WorkWechatContact(tenant_id=1, channel_id=1, contact_kind="friend", contact_external_id=f"wxid_{i}"))
    db.commit()
    channels = friend_check.list_check_channels(db)
    assert {row["id"]: row["total"] for row in channels} == {1: 41, 2: 1}
    contacts = friend_check.list_check_contacts(db, db.get(Channel, 1))
    assert len(contacts) == 41
    assert {row["id"] for row in contacts}.isdisjoint({2, 3, 4, 5})
    assert all("raw_contact_json" not in row and "pay_name_last_char" not in row for row in contacts)


def test_every_check_is_a_new_forced_query_and_keeps_contact_cache_untouched(db, monkeypatch):
    notified = []
    monkeypatch.setattr(friend_check.wakeup, "notify", notified.append)
    first = _start(db)
    first_command = parse_job_payload(first)["command"]
    assert first_command["type"] == "friend_pay_name"
    assert first_command["params"] == {"id": "wxid_friend", "refresh": True}
    assert first.related_type == friend_check.RELATED_TYPE and first.related_id == 1
    assert friend_check.poll_check(db, db.get(Channel, 1), first.id) == {"state": "pending", "result": None}
    _finish(db, first, _payload())
    second = _start(db)
    assert second.id != first.id
    assert parse_job_payload(second)["command"]["request_id"] != first_command["request_id"]
    assert second.result_json is None and first.dedupe_key is None
    assert notified == [1, 1]
    assert json.loads(db.get(WorkWechatContact, 1).raw_contact_json)["pay_name_last_char"] == "旧"


@pytest.mark.parametrize("payload,category", [
    (_payload(), "friend"),
    (_payload(ok=1), "friend"),
    (_payload("unavailable", ""), "deleted"),
    (_payload("failed", "", ok=False, error="网络失败"), "unknown"),
    (_payload("unknown", ""), "unknown"),
    (_payload("not_queried", ""), "unknown"),
    (_payload("querying", ""), "unknown"),
    (_payload("ok", ""), "unknown"),
    (_payload("ok", "异常长字段"), "unknown"),
    (_payload(command_name="other_command"), "unknown"),
    (_payload("unavailable", "明"), "unknown"),
    (_payload(ok="true"), "unknown"),
    (_payload(ok=False), "unknown"),
    (_payload(error="不支持此接口"), "unknown"),
    ({"ok": True, "account_id": "wxid_friend", "result": {"pay_name_status": "ok", "pay_name_last_char": "明"}}, "unknown"),
    ({"ok": True, "result": {"account_id": "wrong", "pay_name_status": "ok", "pay_name_last_char": "明"}}, "unknown"),
    ({"ok": True, "result": []}, "unknown"),
    ({"ok": True, "result": {"ok": True, "result": _payload()["result"]}}, "friend"),
    ({"ok": True, "result": {"ok": False, "result": _payload()["result"]}}, "unknown"),
])
def test_protocol_results_are_classified_conservatively(db, payload, category):
    result = _finish(db, _start(db), payload)
    assert result["state"] == "completed"
    assert result["result"]["category"] == category
    assert result["result"]["contact"] == {"id": 1, "external_id": "wxid_friend", "name": "小明", "remark": "好友备注", "avatar_url": "https://example.test/avatar.png"}
    assert bool(result["result"]["pay_name_last_char"]) == (category == "friend")


@pytest.mark.parametrize("raw", ["not-json", "[]", "null", '{}'])
def test_bad_result_is_unknown(db, raw):
    job = _start(db)
    job.status = "completed"
    job.result_json = raw
    db.commit()
    assert friend_check.poll_check(db, db.get(Channel, 1), job.id)["result"]["category"] == "unknown"


def test_failed_task_cannot_be_classified_as_friend(db):
    result = _finish(db, _start(db), _payload(), status="failed")
    assert result["result"]["category"] == "unknown"


def test_disabled_account_blocks_new_checks_but_allows_polling_existing_result(db):
    job = _start(db)
    channel = db.get(Channel, 1)
    channel.is_enabled = False
    db.commit()
    assert _finish(db, job, _payload())["result"]["category"] == "friend"
    with pytest.raises(friend_check.FriendCheckError) as error:
        _start(db)
    assert error.value.status_code == 400


@pytest.mark.parametrize("state", ["pending", "running"])
def test_timeout_terminalizes_task_and_allows_next_fresh_check(db, state):
    job = _start(db)
    job.status = state
    job.created_at = now_shanghai() - timedelta(seconds=70)
    if state == "running":
        job.locked_at = job.created_at
    db.commit()
    result = friend_check.poll_check(db, db.get(Channel, 1), job.id)
    assert result["result"]["category"] == "unknown"
    assert "超时" in result["result"]["reason"] and job.status == "failed"
    assert _start(db).id != job.id


def test_running_deadline_starts_when_phone_claims_task(db):
    job = _start(db)
    job.status = "running"
    job.created_at = now_shanghai() - timedelta(seconds=70)
    job.locked_at = now_shanghai()
    db.commit()
    assert friend_check.poll_check(db, db.get(Channel, 1), job.id) == {"state": "running", "result": None}


def test_only_one_active_check_per_channel_but_channels_are_independent(db):
    first = _start(db)
    with pytest.raises(friend_check.FriendCheckError) as error:
        _start(db)
    assert error.value.status_code == 409
    second = friend_check.start_check(db, db.get(Channel, 2), 2)
    assert first.id != second.id


def test_unique_key_rejects_a_racing_start_after_initial_lookup(db, monkeypatch):
    enqueue = friend_check.service.enqueue_command

    def competing_start(*args, **kwargs):
        with db.info["test_factory"]() as competing:
            competing.add(TaskJob(tenant_id=1, job_type=JOB_TYPE, status="pending", related_type=friend_check.RELATED_TYPE, related_id=1, dedupe_key="friend-check-active:1", payload_json='{"channel_id":1}'))
            competing.commit()
        return enqueue(*args, **kwargs)

    monkeypatch.setattr(friend_check.service, "enqueue_command", competing_start)
    with pytest.raises(friend_check.FriendCheckError) as error:
        _start(db)
    assert error.value.status_code == 409
    assert len(db.scalars(select(TaskJob)).all()) == 1


@pytest.mark.parametrize("contact_id", [2, 3, 4, 5, 999])
def test_start_rejects_other_channels_tenants_deleted_and_nonfriend_contacts(db, contact_id):
    with pytest.raises(friend_check.FriendCheckError) as error:
        friend_check.start_check(db, db.get(Channel, 1), contact_id)
    assert error.value.status_code == 404


def test_poll_rejects_wrong_channel_and_tampered_command(db):
    job = _start(db)
    with pytest.raises(friend_check.FriendCheckError) as error:
        friend_check.poll_check(db, db.get(Channel, 2), job.id)
    assert error.value.status_code == 404
    payload = parse_job_payload(job)
    payload["command"]["params"]["refresh"] = False
    job.payload_json = json.dumps(payload)
    db.commit()
    with pytest.raises(friend_check.FriendCheckError):
        friend_check.poll_check(db, db.get(Channel, 1), job.id)
    with tenant_scope(2):
        with pytest.raises(friend_check.FriendCheckError) as error:
            friend_check.list_check_contacts(db, Channel(id=1, tenant_id=1, channel_type="wechat_webridge"))
        assert error.value.status_code == 404


def _prepare_claim(db, monkeypatch):
    identity = {"device_id": "test-device", "session_id": "test-session", "account_id": "wxid_logged_in"}
    channel = db.get(Channel, 1)
    channel.runtime_info_json = json.dumps({"webridge": identity})
    db.commit()
    monkeypatch.setattr(friend_check.service, "_poll_maintenance", lambda *args: None)
    return channel, identity


def test_claim_does_not_revive_a_friend_check_expired_after_candidates_were_read(db, monkeypatch):
    job = _start(db)
    job.created_at = now_shanghai() - timedelta(seconds=70)
    db.commit()
    channel, identity = _prepare_claim(db, monkeypatch)
    parse = friend_check.service.parse_job_payload
    expired_results = []

    def expire_after_select(candidate):
        data = parse(candidate)
        if candidate.id == job.id and not expired_results:
            # 候选任务已经被手机轮询读出；另一请求此时真实执行超时收尾。
            with tenant_session(db.info["test_factory"]) as other:
                expired_results.append(friend_check.poll_check(other, other.get(Channel, 1), job.id))
        return data

    monkeypatch.setattr(friend_check.service, "parse_job_payload", expire_after_select)
    commands = friend_check.service.claim_commands(db, channel, identity)
    assert expired_results[0]["result"]["category"] == "unknown"
    assert commands == []
    db.refresh(job)
    assert job.status == "failed" and job.attempts == 0


def test_only_one_claim_can_win_even_when_both_read_pending_candidates(db, monkeypatch):
    job = _start(db)
    channel, identity = _prepare_claim(db, monkeypatch)
    parse = friend_check.service.parse_job_payload
    second_commands = []
    competing = False

    def competing_claim_after_select(candidate):
        nonlocal competing
        data = parse(candidate)
        if candidate.id == job.id and not competing:
            competing = True
            with tenant_session(db.info["test_factory"]) as other:
                second_commands.extend(friend_check.service.claim_commands(other, other.get(Channel, 1), identity))
        return data

    monkeypatch.setattr(friend_check.service, "parse_job_payload", competing_claim_after_select)
    assert friend_check.service.claim_commands(db, channel, identity) == []
    assert len(second_commands) == 1
    db.refresh(job)
    assert job.status == "running" and job.attempts == 1


def test_normal_friend_check_claim_is_returned_only_once(db, monkeypatch):
    job = _start(db)
    channel, identity = _prepare_claim(db, monkeypatch)
    commands = friend_check.service.claim_commands(db, channel, identity)
    assert len(commands) == 1
    assert commands[0]["command_name"] == "friend_pay_name"
    assert commands[0]["args"] == {"id": "wxid_friend", "refresh": True}
    assert friend_check.service.claim_commands(db, channel, identity) == []
    db.refresh(job)
    assert job.status == "running" and job.attempts == 1
    assert job.started_at is not None and job.locked_at is not None
