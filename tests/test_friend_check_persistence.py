"""好友检测的队列、结果及清空边界必须跨请求和服务重启保留。"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import timedelta

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="friend-check-persistence-logs-"))

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, FriendCheckItem, FriendCheckRun, TaskJob, WorkWechatContact
from app.services.wechat_webridge import friend_check, friend_check_runs as runs, service
from app.services.wechat_webridge.contract import JOB_TYPE


IDENTITY = {"device_id": "check-device", "session_id": "check-session", "account_id": "check-account"}


@pytest.fixture()
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'checks.db'}")
    for model in (Channel, WorkWechatContact, TaskJob, FriendCheckRun, FriendCheckItem):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add_all(Channel(
            id=channel_id, tenant_id=tenant_id, bot_id=1, name=f"账号{channel_id}",
            channel_type="wechat_webridge", is_enabled=True,
            runtime_info_json=json.dumps({"webridge": IDENTITY}),
        ) for channel_id, tenant_id in ((1, 1), (2, 1), (3, 2)))
        db.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


@contextmanager
def scoped(factory, tenant_id=1, channel_id=1):
    with tenant_scope(tenant_id), tenant_session(factory) as db:
        yield db, db.get(Channel, channel_id)


def add_contacts(factory, total=3, *, channel_id=1, tenant_id=1):
    with factory() as db:
        contacts = [WorkWechatContact(
            tenant_id=tenant_id, channel_id=channel_id, contact_kind="friend",
            contact_external_id=f"friend-{index}", name=f"好友{index:04d}",
            display_name=f"好友{index:04d}", avatar_url=f"https://example.test/{index}.png",
        ) for index in range(total)]
        db.add_all(contacts)
        db.commit()
        return [int(contact.id) for contact in contacts]


def begin(factory, *, channel_id=1, tenant_id=1):
    with scoped(factory, tenant_id, channel_id) as (db, channel):
        state = runs.get_state(db, channel)
        return runs.start_run(db, channel, generation=state["generation"], interval_seconds=1)


def finish(factory, job_id, *, status="ok", action=None, remark="更新后的备注", elapsed_seconds=None):
    with factory() as db:
        job = db.get(TaskJob, job_id)
        payload = json.loads(job.payload_json)
        command = payload["command"]
        args = command.get("params") or command.get("args") or {}
        if action == "remark":
            result = {"id": args["id"], "status": "applied_locally", "remark": remark}
        elif action == "delete":
            result = {"id": args["id"]}
        else:
            result = {"account_id": args["id"], "pay_name_status": status,
                      "pay_name_last_char": "张" if status == "ok" else ""}
        job.status = "completed"
        job.finished_at = now_shanghai()
        if elapsed_seconds is not None:
            job.created_at = job.started_at = job.finished_at - timedelta(seconds=elapsed_seconds)
        job.result_json = json.dumps({"webridge": {
            "ok": True, "command_name": command["type"], "result": result,
        }}, ensure_ascii=False)
        db.commit()


def add_legacy_job(db, contact_id, *, channel_id=1, tenant_id=1, status="ok", target=None, command="friend_pay_name"):
    contact = db.get(WorkWechatContact, contact_id)
    external_id = target or contact.contact_external_id
    job = TaskJob(
        tenant_id=tenant_id, job_type=JOB_TYPE, status="completed",
        related_type=friend_check.RELATED_TYPE, related_id=contact_id,
        payload_json=json.dumps({"channel_id": channel_id, "command": {
            "request_id": f"legacy-{channel_id}-{tenant_id}-{contact_id}", "type": command,
            "params": {"id": external_id, "refresh": True},
        }}),
        result_json=json.dumps({"webridge": {"ok": True, "command_name": command, "result": {
            "account_id": external_id, "pay_name_status": status,
            "pay_name_last_char": "王" if status == "ok" else "",
        }}}, ensure_ascii=False),
        finished_at=now_shanghai(),
    )
    db.add(job)
    db.flush()
    return int(job.id)


def test_pending_check_survives_new_session_and_repeated_start_is_idempotent(store):
    """浏览器没有收到创建响应时，继续同一位只能找回已有任务，不能重复发命令。"""

    _engine, factory = store
    contact_ids = add_contacts(factory)
    state = begin(factory)
    with scoped(factory) as (db, channel):
        first = runs.start_item(db, channel, contact_ids[0], generation=state["generation"])
    with scoped(factory) as (db, channel):
        restored = runs.get_state(db, channel)
        repeated = runs.start_item(db, channel, contact_ids[0], generation=state["generation"])
        assert repeated["job_id"] == first["job_id"]
        assert restored["pending"]["id"] == first["job_id"]
        assert restored["pending"]["action"] == "scan"
        assert restored["cursor"] == 0 and restored["total"] == 3
        assert db.scalar(select(func.count()).select_from(TaskJob)) == 1


def test_completed_result_is_recovered_without_browser_poll_and_pause_keeps_it(store):
    """关闭页面后手机才回报，重新打开也必须补记结果，并从未完成好友继续。"""

    _engine, factory = store
    ids = add_contacts(factory)
    state = begin(factory)
    with scoped(factory) as (db, channel):
        task = runs.start_item(db, channel, ids[0], generation=state["generation"])
    finish(factory, task["job_id"], elapsed_seconds=3)
    with scoped(factory) as (db, channel):
        restored = runs.get_state(db, channel)
        assert restored["cursor"] == 1
        assert restored["pending"] is None
        assert len(restored["results"]) == 1
        assert restored["results"][0]["contact"]["id"] == ids[0]
        assert restored["results"][0]["category"] == "friend"
        assert restored["results"][0]["pay_name_last_char"] == "张"
        assert restored["elapsed_ms"] >= 3000
        paused = runs.pause_run(db, channel, generation=state["generation"])
        assert paused["results"] == restored["results"]
    resumed = begin(factory)
    assert resumed["generation"] == state["generation"]
    assert resumed["cursor"] == 1 and len(resumed["results"]) == 1
    assert resumed["elapsed_ms"] >= 3000
    with scoped(factory) as (db, channel):
        assert db.scalar(select(func.count()).select_from(TaskJob)) == 1
        next_task = runs.start_item(db, channel, ids[1], generation=state["generation"])
        assert next_task["job_id"] != task["job_id"]


def test_clear_requires_explicit_confirmation_and_changes_persistent_generation(store):
    _engine, factory = store
    ids = add_contacts(factory)
    with factory() as db:
        add_legacy_job(db, ids[0])
        db.commit()
    with scoped(factory) as (db, channel):
        initial = runs.get_state(db, channel)
        assert len(initial["results"]) == 1
        with pytest.raises(friend_check.FriendCheckError) as error:
            runs.clear_run(db, channel, generation=initial["generation"], confirmed=False)
        assert error.value.status_code == 400
        assert len(runs.get_state(db, channel)["results"]) == 1
        cleared = runs.clear_run(db, channel, generation=initial["generation"], confirmed=True)
        assert cleared["generation"] != initial["generation"]
        assert cleared["cursor"] == 0 and cleared["results"] == []
        assert cleared["contacts"] is None
    with scoped(factory) as (db, channel):
        restored = runs.get_state(db, channel)
        assert restored["generation"] == cleared["generation"]
        assert restored["results"] == []
        assert db.scalar(select(func.count()).select_from(TaskJob)) == 1
        with pytest.raises(friend_check.FriendCheckError) as error:
            runs.start_run(db, channel, generation=initial["generation"])
        assert error.value.status_code == 409


def test_clear_during_pending_check_does_not_restore_a_late_result(store):
    _engine, factory = store
    ids = add_contacts(factory)
    state = begin(factory)
    with scoped(factory) as (db, channel):
        task = runs.start_item(db, channel, ids[0], generation=state["generation"])
        cleared = runs.clear_run(db, channel, generation=state["generation"], confirmed=True)
    with scoped(factory) as (db, channel):
        job = db.get(TaskJob, task["job_id"])
        job.status = "running"
        db.commit()
        request_id = json.loads(job.payload_json)["command"]["request_id"]
        assert service.accept_command_result(db, channel, {
            **IDENTITY, "request_id": request_id, "ok": True,
            "result": {"account_id": "friend-0", "pay_name_status": "ok", "pay_name_last_char": "张"},
        })["status"] == "completed"
    with scoped(factory) as (db, channel):
        with pytest.raises(friend_check.FriendCheckError) as error:
            runs.poll_item(db, channel, task["job_id"], generation=state["generation"])
        assert error.value.status_code == 409
        restored = runs.get_state(db, channel)
        assert restored["generation"] == cleared["generation"]
        assert restored["results"] == [] and restored["cursor"] == 0


def test_recheck_updates_saved_classification_without_advancing_scan_progress(store):
    _engine, factory = store
    ids = add_contacts(factory, 1)
    with factory() as db:
        add_legacy_job(db, ids[0], status="unavailable")
        db.commit()
    with scoped(factory) as (db, channel):
        before = runs.get_state(db, channel)
        assert before["results"][0]["category"] == "deleted"
        task = runs.start_item(db, channel, ids[0], generation=before["generation"], scan=False)
        assert runs.get_state(db, channel)["pending"]["action"] == "recheck"
    finish(factory, task["job_id"])
    with scoped(factory) as (db, channel):
        restored = runs.get_state(db, channel)
        assert restored["cursor"] == before["cursor"] == 1
        assert restored["total"] == before["total"] == 1
        assert len(restored["results"]) == 1
        assert restored["results"][0]["category"] == "deleted"
        assert restored["results"][0]["recheckCategory"] == "friend"
        assert restored["results"][0]["pay_name_last_char"] == "张"


def test_recheck_cannot_reuse_a_pending_remark_task(store):
    """同一好友尚在改备注时，复查入口不能把备注任务冒充检测任务返回。"""

    _engine, factory = store
    ids = add_contacts(factory, 1)
    with factory() as db:
        add_legacy_job(db, ids[0])
        db.commit()
    with scoped(factory) as (db, channel):
        state = runs.get_state(db, channel)
        original = runs.start_action(db, channel, ids[0], generation=state["generation"], action="remark")
        with pytest.raises(friend_check.FriendCheckError) as error:
            runs.start_item(db, channel, ids[0], generation=state["generation"], scan=False)
        assert error.value.status_code == 409
        db.rollback()
        restored = runs.get_state(db, channel)
        assert restored["pending"]["id"] == original["job_id"]
        assert restored["pending"]["action"] == "remark"


def test_recheck_cannot_skip_an_unchecked_friend_in_the_scan_queue(store):
    """复查只允许已有结果的好友，不能借此越过队列推进持久进度。"""

    _engine, factory = store
    ids = add_contacts(factory)
    state = begin(factory)
    with scoped(factory) as (db, channel):
        with pytest.raises(friend_check.FriendCheckError) as error:
            runs.start_item(db, channel, ids[-1], generation=state["generation"], scan=False)
        assert error.value.status_code == 409
        db.rollback()
        restored = runs.get_state(db, channel)
        assert restored["cursor"] == 0 and restored["results"] == []
        assert restored["pending"] is None
        assert db.scalar(select(func.count()).select_from(TaskJob)) == 0


def test_history_import_uses_latest_matching_result_and_does_not_cross_scopes(store):
    _engine, factory = store
    ids = add_contacts(factory)
    with factory() as db:
        add_legacy_job(db, ids[0], status="ok")
        add_legacy_job(db, ids[0], status="unavailable")
        add_legacy_job(db, ids[0], channel_id=2)
        add_legacy_job(db, ids[0], tenant_id=2)
        add_legacy_job(db, ids[1], target="wrong-friend")
        add_legacy_job(db, ids[2], command="friends_all")
        db.commit()
    with scoped(factory) as (db, channel):
        state = runs.get_state(db, channel)
        assert state["cursor"] == 1
        assert len(state["results"]) == 1
        assert state["results"][0]["contact"]["id"] == ids[0]
        assert state["results"][0]["category"] == "deleted"
    # 初始化边界已经保存，后来出现的旧式任务不会被再次当作整轮恢复来源。
    with factory() as db:
        add_legacy_job(db, ids[1])
        db.commit()
    with scoped(factory) as (db, channel):
        assert runs.get_state(db, channel)["cursor"] == 1


def test_starting_a_completed_run_never_clears_saved_results(store):
    _engine, factory = store
    ids = add_contacts(factory, 1)
    with factory() as db:
        add_legacy_job(db, ids[0])
        db.commit()
    with scoped(factory) as (db, channel):
        state = runs.get_state(db, channel)
        assert state["cursor"] == state["total"] == 1
        repeated = runs.start_run(db, channel, generation=state["generation"])
        assert repeated["generation"] == state["generation"]
        assert repeated["cursor"] == repeated["total"] == 1
        assert repeated["results"] == state["results"]
        assert db.scalar(select(func.count()).select_from(TaskJob)) == 1


def test_channels_and_tenants_keep_independent_runs(store):
    _engine, factory = store
    ids = add_contacts(factory)
    add_contacts(factory, 2, channel_id=2)
    add_contacts(factory, 1, channel_id=3, tenant_id=2)
    first, second, third = begin(factory), begin(factory, channel_id=2), begin(factory, channel_id=3, tenant_id=2)
    assert len({first["generation"], second["generation"], third["generation"]}) == 3
    with scoped(factory) as (db, channel):
        task = runs.start_item(db, channel, ids[0], generation=first["generation"])
    finish(factory, task["job_id"])
    with scoped(factory) as (db, channel):
        assert runs.get_state(db, channel)["cursor"] == 1
        runs.clear_run(db, channel, generation=first["generation"], confirmed=True)
    with scoped(factory, channel_id=2) as (db, channel):
        state = runs.get_state(db, channel)
        assert state["generation"] == second["generation"] and state["total"] == 2
        assert state["cursor"] == 0
    with scoped(factory, tenant_id=2, channel_id=3) as (db, channel):
        assert runs.get_state(db, channel)["generation"] == third["generation"]


def test_2600_saved_results_restore_with_bounded_selects(store):
    """恢复大批结果必须批量读取，不能再次逐位查询联系人和历史任务。"""

    engine, factory = store
    ids = add_contacts(factory, 2600)
    with factory() as db:
        for contact_id in ids:
            add_legacy_job(db, contact_id)
        db.commit()
    with scoped(factory) as (db, channel):
        assert runs.get_state(db, channel)["cursor"] == 2600
    selects = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with scoped(factory) as (db, channel):
            restored = runs.get_state(db, channel)
            assert restored["cursor"] == 2600 and len(restored["results"]) == 2600
        assert len(selects) <= 12
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_check_result_does_not_load_all_historical_commands(store):
    """保留好友操作互斥的同时，回报应直接定位任务，不因历史变多而变慢。"""

    _engine, factory = store
    ids = add_contacts(factory, 1)
    state = begin(factory)
    with factory() as db:
        for index in range(80):
            add_legacy_job(db, ids[0], target=f"history-{index}")
        db.commit()
    with scoped(factory) as (db, channel):
        task = runs.start_item(db, channel, ids[0], generation=state["generation"])
    with factory() as db:
        job = db.get(TaskJob, task["job_id"])
        job.status = "running"
        request_id = json.loads(job.payload_json)["command"]["request_id"]
        db.commit()
    with scoped(factory) as (db, channel):
        loaded = []

        def record(_session, instance):
            if isinstance(instance, TaskJob):
                loaded.append(instance.id)

        event.listen(db, "loaded_as_persistent", record)
        result = service.accept_command_result(db, channel, {
            **IDENTITY, "request_id": request_id, "ok": True,
            "result": {"account_id": "friend-0", "pay_name_status": "ok", "pay_name_last_char": "张"},
        })
        assert result["job_id"] == task["job_id"] and result["status"] == "completed"
        assert set(loaded) == {task["job_id"]}


@pytest.mark.parametrize("action", ["remark", "delete"])
def test_finished_friend_action_is_kept_in_saved_result_after_reopening(store, action):
    _engine, factory = store
    ids = add_contacts(factory, 1)
    with factory() as db:
        add_legacy_job(db, ids[0], status="unavailable")
        db.commit()
    with scoped(factory) as (db, channel):
        state = runs.get_state(db, channel)
        task = runs.start_action(db, channel, ids[0], generation=state["generation"], action=action)
    finish(factory, task["job_id"], action=action)
    with scoped(factory) as (db, channel):
        restored = runs.get_state(db, channel)
        item = restored["results"][0]
        assert item["contact"]["id"] == ids[0]
        assert item["category"] == "deleted"
        if action == "remark":
            assert item["contact"]["remark"] == "更新后的备注"
        else:
            assert item["removed"] is True
        assert restored["pending"] is None


def test_cleared_job_cannot_be_polled_with_new_generation(store):
    """旧结果即使已经由手机完成，也不能重新进入已清空的检测记录。"""
    _engine, factory = store
    ids = add_contacts(factory, 1)
    state = begin(factory)
    with scoped(factory) as (db, channel):
        task = runs.start_item(db, channel, ids[0], generation=state["generation"])
        cleared = runs.clear_run(db, channel, generation=state["generation"], confirmed=True)
    finish(factory, task["job_id"])
    with scoped(factory) as (db, channel):
        with pytest.raises(friend_check.FriendCheckError) as error:
            runs.poll_item(db, channel, task["job_id"], generation=cleared["generation"])
        assert error.value.status_code == 404
        assert runs.get_state(db, channel)["results"] == []
