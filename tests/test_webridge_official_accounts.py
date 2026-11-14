"""公众号必须走专用接口,其目录结果供消息页和历史消息共用。"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Conversation, Message, TaskJob, WorkWechatContact
from app.services.identity_directory import IdentityDirectory
from app.services.task_queue import parse_job_payload
from app.services.wechat_webridge import official_accounts as directory
from app.services.wechat_webridge import service
from test_provider_node_pages import _login, provider_page_context  # noqa: F401

IDENTITY = {"device_id": "official-device", "session_id": "official-session", "account_id": "wxid_self"}
PROFILE = {"account_id": "daily_news", "nick_name": "每日资讯", "display_name": "资讯订阅", "avatar_url": "https://example.test/official.png", "account_type": "subscription", "is_followed": True}


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(service, "_last_poll_maintenance", {})
    with tenant_scope(1), tenant_session(factory) as db:
        bot = Bot(name="公众号测试", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(bot_id=bot.id, name="微桥", channel_type="wechat_webridge", is_enabled=True, runtime_info_json=json.dumps({"webridge": IDENTITY}))
        db.add(channel)
        db.flush()
        yield db, channel
    engine.dispose()


def commands(db, channel):
    return [job for job in db.scalars(select(TaskJob).order_by(TaskJob.id)).all()
            if (data := parse_job_payload(job)).get("channel_id") == channel.id
            and data.get("command", {}).get("type") == "official_accounts"]


def finish(db, channel, job, items, *, ok=True, nested=False, overrides=None, **extra):
    job.status = "running"
    db.flush()
    result = {"status": "ok", "items": items, "has_more": False, **extra}
    result.update(overrides or {})
    payload = {**IDENTITY, "request_id": parse_job_payload(job)["command"]["request_id"], "ok": ok, "result": {"result": result} if nested else result}
    response = service.accept_command_result(db, channel, payload)
    db.flush()
    return response, payload


@pytest.mark.parametrize("external_id", ["gh_dailynews", "daily_news"])
def test_dedicated_directory_enriches_existing_conversations_and_messages(env, external_id):
    db, channel = env
    conversation = Conversation(bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type, external_id=external_id, title=external_id, chat_type="direct")
    db.add(conversation)
    db.flush()
    message = Message(conversation_id=conversation.id, direction="in", sender_id=external_id, sender_name=external_id, content="历史推送", message_type="official_account")
    db.add(message)
    db.flush()
    service.persist_directory_result(db, channel, {"type": "official_accounts"}, {"items": [{**PROFILE, "account_id": external_id}]})
    contact = db.scalar(select(WorkWechatContact))
    assert contact.contact_kind == "official"
    assert contact.name == "每日资讯" and contact.display_name == "资讯订阅"
    assert json.loads(contact.raw_contact_json)["account_type"] == "subscription"
    directory_view = IdentityDirectory(db)
    for identity in (directory_view.for_conversations([conversation])[conversation.id], directory_view.for_messages([message])[message.id]):
        assert identity["name"] == "资讯订阅" and identity["avatar_url"] == PROFILE["avatar_url"]
    assert conversation.title == "资讯订阅" and message.sender_name == external_id


def test_poll_automatically_requests_all_account_types_with_integer_paging(env):
    db, channel = env
    service.claim_commands(db, channel, IDENTITY)
    claimed = service.claim_commands(db, channel, IDENTITY)
    command = next(item for item in claimed if item["command_name"] == "official_accounts")
    assert command["args"] == {"type": "all", "limit": 100, "offset": 0}
    assert directory.request_sync(db, channel, force=True).id == commands(db, channel)[0].id
    assert len(commands(db, channel)) == 1


@pytest.mark.parametrize("has_more,finished", [(True, False), (1, 0)])
def test_sync_continues_actual_page_length_and_deduplicates_results(env, has_more, finished):
    db, channel = env
    first = directory.request_sync(db, channel)
    response, payload = finish(db, channel, first, [PROFILE], has_more=has_more, offset=0, total=2)
    assert response["status"] == "completed"
    jobs = commands(db, channel)
    assert len(jobs) == 2 and parse_job_payload(jobs[-1])["command"]["params"]["offset"] == 1
    assert "synced_at" not in service._runtime(channel)[directory.STATE_KEY]
    assert service.accept_command_result(db, channel, payload)["deduplicated"] is True
    assert len(commands(db, channel)) == 2
    finish(db, channel, jobs[-1], [{**PROFILE, "account_id": "service_b", "account_type": "service"}], offset=1, nested=True, has_more=finished)
    assert "synced_at" in service._runtime(channel)[directory.STATE_KEY]
    assert len(db.scalars(select(WorkWechatContact)).all()) == 2
    assert directory.request_sync(db, channel) is None
    state = service._runtime(channel)
    state[directory.STATE_KEY] = {"synced_at": (now_shanghai() - timedelta(seconds=directory.REFRESH_SECONDS + 1)).isoformat()}
    service._save_runtime(channel, state)
    assert directory.request_sync(db, channel) is not None
    assert len(commands(db, channel)) == 3


@pytest.mark.parametrize("ok,result_extra", [(False, {}), (True, {"ok": False, "status": "failed", "error": "CONTACTS_NOT_READY"}), (True, {"items": None})])
def test_failed_or_malformed_result_keeps_profiles_and_retries_after_backoff(env, ok, result_extra):
    db, channel = env
    service.persist_directory_result(db, channel, {"type": "official_accounts"}, {"items": [PROFILE]})
    first = directory.request_sync(db, channel)
    response, _ = finish(db, channel, first, [], ok=ok, overrides=result_extra)
    assert response["status"] == "failed"
    assert db.scalar(select(WorkWechatContact)).display_name == "资讯订阅"
    assert "synced_at" not in service._runtime(channel)[directory.STATE_KEY]
    assert directory.request_sync(db, channel) is None
    state = service._runtime(channel)
    state[directory.STATE_KEY]["requested_at"] = (now_shanghai() - timedelta(seconds=directory.RETRY_SECONDS + 1)).isoformat()
    service._save_runtime(channel, state)
    assert directory.request_sync(db, channel) is not None
    assert len(commands(db, channel)) == 2


@pytest.mark.parametrize("result_extra", [{"has_more": True}, {"offset": 123}])
def test_empty_nonfinal_page_or_wrong_offset_cannot_finish_or_loop(env, result_extra):
    db, channel = env
    job = directory.request_sync(db, channel)
    finish(db, channel, job, [], **result_extra)
    assert len(commands(db, channel)) == 1
    assert "synced_at" not in service._runtime(channel)[directory.STATE_KEY]


def test_generic_filtered_query_saves_profiles_without_expanding_query(env):
    db, channel = env
    job = service.enqueue_command(db, channel, command_name="official_accounts", args={"type": "subscription", "keyword": "资讯"})
    finish(db, channel, job, [PROFILE], has_more=True)
    assert len(commands(db, channel)) == 1
    assert db.scalar(select(WorkWechatContact)).contact_kind == "official"
    assert directory.STATE_KEY not in service._runtime(channel)


def test_sync_is_scoped_to_channel_and_skips_unconfirmed_accounts(env):
    db, channel = env
    directory.request_sync(db, channel)
    other = Channel(bot_id=channel.bot_id, name="另一账号", channel_type="wechat_webridge", is_enabled=False)
    db.add(other)
    db.flush()
    assert directory.request_sync(db, other, force=True) is None
    other.is_enabled = True
    assert directory.request_sync(db, other) is not None
    assert len(commands(db, channel)) == len(commands(db, other)) == 1
    service.persist_directory_result(db, other, {"type": "official_accounts"}, {"items": [PROFILE]})
    assert IdentityDirectory(db).get(channel.id, PROFILE["account_id"])["avatar_url"] == ""
    assert IdentityDirectory(db).get(other.id, PROFILE["account_id"])["avatar_url"] == PROFILE["avatar_url"]


def test_manual_sync_and_message_page_share_official_account_profiles(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    csrf = _login(client, "admin")
    with ids["factory"]() as db:
        channel = db.get(Channel, ids["channel_id"])
        channel.channel_type = "wechat_webridge"
        channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
        conversation = Conversation(tenant_id=ids["alpha"], bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type, external_id=PROFILE["account_id"], chat_type="direct", title=PROFILE["account_id"])
        db.add(conversation)
        db.flush()
        message = Message(tenant_id=ids["alpha"], conversation_id=conversation.id, direction="in", sender_id=PROFILE["account_id"], sender_name=PROFILE["account_id"], content="历史推送", message_type="official_account")
        db.add(message)
        db.commit()
        conversation_id = conversation.id
    url = f"/channels/{ids['channel_id']}/wechat-webridge"
    assert "同步公众号" in client.get(url).text
    for _ in range(2):
        assert client.post(url + "/command", data={"csrf_token": csrf, "command_name": "official_accounts", "args_json": "{}"}).status_code == 303
    with tenant_scope(ids["alpha"]), tenant_session(ids["factory"]) as db:
        channel = db.get(Channel, ids["channel_id"])
        jobs = commands(db, channel)
        assert len(jobs) == 1
        finish(db, channel, jobs[0], [PROFILE])
    page = client.get(f"/messages?conversation_id={conversation_id}")
    history = client.get(f"/messages/history?conversation_id={conversation_id}")
    assert page.status_code == history.status_code == 200
    for html in (page.text, history.json()["html"]):
        assert "资讯订阅" in html and PROFILE["avatar_url"] in html
    assert "每日资讯" not in client.get(url + "?tab=contacts").text
    official = client.get("/messages/conversations?chat_type=official").json()["conversations"]
    assert any(item["id"] == conversation_id and item["kind"] == "official" for item in official)
    direct = client.get("/messages/conversations?chat_type=direct").json()["conversations"]
    assert all(item["id"] != conversation_id for item in direct)


@pytest.mark.parametrize("wrapper_ok", [True, False])
def test_nested_response_preserves_explicit_failure_and_rejects_invalid_items(env, wrapper_ok):
    """异常条目与外层失败不能在解包后变成成功的目录快照。"""
    db, channel = env
    job = directory.request_sync(db, channel)
    job.status = "running"
    db.flush()  # 夹具关闭自动 flush，模拟手机已领取后的数据库状态。
    result = {"ok": True, "status": "ok", "items": [None] if wrapper_ok else [PROFILE], "has_more": False}
    response = service.accept_command_result(db, channel, {
        **IDENTITY, "request_id": parse_job_payload(job)["command"]["request_id"],
        "ok": True, "result": {"ok": wrapper_ok, "result": result},
    })
    assert response["status"] == "failed"
    assert "synced_at" not in service._runtime(channel)[directory.STATE_KEY]
    assert db.scalar(select(WorkWechatContact)) is None
