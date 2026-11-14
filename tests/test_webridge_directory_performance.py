"""验证好友批量同步的查询规模、资料隔离和回报线程边界。"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-directory-logs-"))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.api.wechat_webridge as bridge_api
from app.core.tenant_context import get_current_tenant_id, require_tenant_id, tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, Conversation, TaskJob, WorkWechatContact
from app.services.wechat_webridge import service


IDENTITY = {"device_id": "test-device", "session_id": "test-session", "account_id": "test-account"}


@pytest.fixture()
def directory_store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'directory.db'}")
    for model in (Channel, WorkWechatContact, Conversation, TaskJob):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(Channel(
            id=1, tenant_id=1, bot_id=1, name="同步测试账号", channel_type="wechat_webridge",
            runtime_info_json=json.dumps({"webridge": IDENTITY}),
        ))
        db.commit()
    try:
        yield engine, factory
    finally:
        engine.dispose()


@contextmanager
def _selects(engine):
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize("command_name", ["friends", "friends_all"])
def test_friend_sync_keeps_field_mapping_and_does_not_touch_other_scopes(directory_store, command_name):
    """相同微信号在其他渠道和租户下的资料、会话标题均不能被本次同步覆盖。"""

    _engine, factory = directory_store
    with factory() as db:
        rows = []
        for tenant_id, channel_id in ((1, 1), (1, 2), (2, 1)):
            # 跨租户历史脏数据即使指向当前渠道，也必须被租户会话过滤。
            contact = WorkWechatContact(
                tenant_id=tenant_id, channel_id=channel_id, contact_external_id="shared-id",
                name="旧昵称", display_name="旧备注", avatar_url="old-avatar", source="manual",
            )
            conversation = Conversation(
                tenant_id=tenant_id, bot_id=1, channel_id=channel_id, channel_type="wechat_webridge",
                external_id="shared-id", title="旧会话名",
            )
            db.add_all([contact, conversation])
            rows.append((contact, conversation))
        db.commit()
        saved_ids = [(contact.id, conversation.id) for contact, conversation in rows]

    item = {"account_id": "shared-id", "nick_name": "新昵称", "remark": "新备注", "avatar_url": "new-avatar"}
    with tenant_scope(1), tenant_session(factory) as db:
        channel = db.get(Channel, 1)
        counts = service.persist_directory_result(db, channel, {"type": command_name}, {"items": [item]})
        db.commit()
        assert counts == {"contacts": 1, "groups": 0, "members": 0}

    with factory() as db:
        for index, (contact_id, conversation_id) in enumerate(saved_ids):
            contact = db.get(WorkWechatContact, contact_id)
            conversation = db.get(Conversation, conversation_id)
            if index:
                assert (contact.name, contact.display_name, contact.avatar_url, contact.source) == (
                    "旧昵称", "旧备注", "old-avatar", "manual",
                )
                assert conversation.title == "旧会话名"
            else:
                assert (contact.name, contact.display_name, contact.avatar_url, contact.source) == (
                    "新昵称", "新备注", "new-avatar", "wechat_webridge",
                )
                assert contact.sync_status == "active"
                assert contact.last_seen_at == contact.last_synced_at
                assert json.loads(contact.raw_contact_json) == item
                assert conversation.title == "新备注"


def test_repeated_friend_ids_create_one_contact_and_keep_last_profile(directory_store):
    """插件同一份列表重复回传微信号时，不能创建多条联系人记录。"""

    _engine, factory = directory_store
    with tenant_scope(1), tenant_session(factory) as db:
        channel = db.get(Channel, 1)
        counts = service.persist_directory_result(db, channel, {"type": "friends_all"}, {"friends": [
            {"wxid": "same-id", "nickname": "首次昵称"},
            {"username": "other-id", "display_name": "另一位"},
            {"account_id": "same-id", "nick_name": "最终昵称", "remark": "最终备注", "avatar": "avatar"},
            {"account_id": " "}, {}, "invalid-item",
        ]})
        db.commit()
        contacts = db.scalars(select(WorkWechatContact)).all()
        assert counts["contacts"] == 3
        assert len(contacts) == 2
        repeated = next(contact for contact in contacts if contact.contact_external_id == "same-id")
        assert (repeated.name, repeated.display_name, repeated.avatar_url) == ("最终昵称", "最终备注", "avatar")
        assert repeated.first_seen_at is not None


def test_2600_friends_use_fixed_queries_for_insert_and_update(directory_store):
    """大列表新增和重复同步都保持两次目录读取，避免查询数随好友数量增长。"""

    engine, factory = directory_store
    total = 2600
    with factory() as db:
        db.add_all(Conversation(
            tenant_id=1, bot_id=1, channel_id=1, channel_type="wechat_webridge",
            external_id=f"friend-{index}", title="旧名称",
        ) for index in range(total))
        db.commit()

    for nickname in ("首次资料", "更新资料"):
        with tenant_scope(1), tenant_session(factory) as db:
            channel = db.get(Channel, 1)
            items = [{"account_id": f"friend-{index}", "nick_name": f"{nickname}{index}"} for index in range(total)]
            with _selects(engine) as queries:
                counts = service.persist_directory_result(db, channel, {"type": "friends_all"}, {"items": items})
                db.commit()
            assert counts["contacts"] == total
            assert len(queries) == 2
            assert sum("FROM workwechat_contacts" in statement for statement in queries) == 1
            assert sum("FROM conversations" in statement for statement in queries) == 1
            assert db.scalar(select(func.count()).select_from(WorkWechatContact)) == total
            assert db.scalar(select(Conversation.title).where(Conversation.external_id == "friend-2599")) == f"{nickname}2599"
            assert db.scalar(select(WorkWechatContact.name).where(WorkWechatContact.contact_external_id == "friend-2599")) == f"{nickname}2599"


def test_empty_friend_result_keeps_existing_profiles(directory_store):
    _engine, factory = directory_store
    with tenant_scope(1), tenant_session(factory) as db:
        db.add(WorkWechatContact(channel_id=1, contact_external_id="existing", name="保留资料"))
        db.commit()
        channel = db.get(Channel, 1)
        counts = service.persist_directory_result(db, channel, {"type": "friends_all"}, {"items": []})
        assert counts == {"contacts": 0, "groups": 0, "members": 0}
        assert db.scalar(select(WorkWechatContact.name)) == "保留资料"


def _add_job(db, *, request_id, tenant_id=1, channel_id=1, dedupe=True, status="running"):
    job = TaskJob(
        tenant_id=tenant_id, job_type=service.JOB_TYPE, status=status,
        dedupe_key=f"webridge:{channel_id}:{request_id}" if dedupe else None,
        payload_json=json.dumps({"channel_id": channel_id, "command": {
            "request_id": request_id, "type": "friends_all", "params": {},
        }}),
    )
    db.add(job)
    db.flush()
    return int(job.id)


@pytest.mark.parametrize("dedupe", [True, False], ids=["indexed-request", "legacy-request"])
def test_command_result_matches_target_without_crossing_scopes(directory_store, dedupe):
    """新任务精确查找，旧任务仍可回报；同请求号的其他渠道、租户任务不受影响。"""

    engine, factory = directory_store
    request_id = "cmd_directory_result"
    with factory() as db:
        other_channel = _add_job(db, request_id=request_id, channel_id=2)
        other_tenant = _add_job(db, request_id=request_id, tenant_id=2)
        for index in range(80):
            _add_job(db, request_id=f"cmd_history_{index}", status="completed")
        target_id = _add_job(db, request_id=request_id, dedupe=dedupe)
        db.commit()

    loaded_job_ids = []
    with tenant_scope(1), tenant_session(factory) as db:
        channel = db.get(Channel, 1)

        def record_load(_session, instance):
            if isinstance(instance, TaskJob):
                loaded_job_ids.append(instance.id)

        event.listen(db, "loaded_as_persistent", record_load)
        result = service.accept_command_result(db, channel, {
            **IDENTITY, "request_id": request_id, "ok": True, "result": {"ok": True, "items": []},
        })
        assert result == {"ok": True, "job_id": target_id, "status": "completed"}
        if dedupe:
            assert loaded_job_ids == [target_id]
        else:
            assert target_id in loaded_job_ids

    with factory() as db:
        assert db.get(TaskJob, target_id).status == "completed"
        assert db.get(TaskJob, other_channel).status == "running"
        assert db.get(TaskJob, other_tenant).status == "running"


def test_command_result_keeps_sessions_in_worker_and_event_loop_responsive(directory_store, monkeypatch):
    """回报保存即使暂时阻塞，也不应卡住事件循环或跨线程共享数据库会话。"""

    engine, factory = directory_store
    with factory() as db:
        target_id = _add_job(db, request_id="cmd_worker")
        db.commit()

    main_thread = threading.get_ident()
    trace = []
    entered = threading.Event()
    release = threading.Event()

    class RecordedSession(Session):
        def __init__(self, **kwargs):
            self.created_thread = threading.get_ident()
            super().__init__(**kwargs)
            trace.append(("create", self.created_thread, get_current_tenant_id()))

        def get(self, *args, **kwargs):
            assert threading.get_ident() == self.created_thread
            trace.append(("get", threading.get_ident(), get_current_tenant_id()))
            return super().get(*args, **kwargs)

        def close(self):
            assert threading.get_ident() == self.created_thread
            trace.append(("close", threading.get_ident(), get_current_tenant_id()))
            super().close()

    recorded_factory = sessionmaker(bind=engine, class_=RecordedSession, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(bridge_api, "SessionLocal", recorded_factory)
    monkeypatch.setattr(bridge_api, "TenantSessionLocal", recorded_factory)
    original_accept = service.accept_command_result

    def slow_accept(db, channel, body):
        assert require_tenant_id() == 1
        assert threading.get_ident() != main_thread
        assert db.get(Channel, 1).tenant_id == 1
        entered.set()
        assert release.wait(timeout=3), "事件循环未能在保存期间继续执行"
        return original_accept(db, channel, body)

    monkeypatch.setattr(bridge_api, "accept_command_result", slow_accept)

    async def json_body():
        return {**IDENTITY, "request_id": "cmd_worker", "ok": True, "result": {"ok": True, "items": []}}

    async def run():
        # 请求正文和外层上下文都不能决定回报所属租户，必须从可信渠道重新解析。
        with tenant_scope(999):
            task = asyncio.create_task(bridge_api.command_result(1, SimpleNamespace(json=json_body)))
            try:
                async def wait_for_worker():
                    while not entered.is_set():
                        await asyncio.sleep(0.001)

                await asyncio.wait_for(wait_for_worker(), timeout=2)
                assert not task.done()
            finally:
                release.set()
            assert await task == {"ok": True, "job_id": target_id, "status": "completed"}
            assert require_tenant_id() == 999

    asyncio.run(run())
    assert len([entry for entry in trace if entry[0] == "create"]) == 2
    assert len([entry for entry in trace if entry[0] == "close"]) == 2
    assert all(thread_id != main_thread for _action, thread_id, _tenant_id in trace)
    assert any(action == "create" and tenant_id == 1 for action, _thread, tenant_id in trace)


def test_command_result_preserves_identity_errors_as_conflict(directory_store, monkeypatch):
    _engine, factory = directory_store
    monkeypatch.setattr(bridge_api, "SessionLocal", factory)
    monkeypatch.setattr(bridge_api, "TenantSessionLocal", factory)

    async def json_body():
        return {**IDENTITY, "account_id": "another-account", "request_id": "cmd_invalid"}

    with pytest.raises(HTTPException) as error:
        asyncio.run(bridge_api.command_result(1, SimpleNamespace(json=json_body)))
    assert error.value.status_code == 409
    assert error.value.detail == "webridge_identity_mismatch"
