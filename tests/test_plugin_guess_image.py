from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message, Plugin, RawWebhookEvent, TaskJob
from app.models.saas import Tenant
from app.services import plugin_directory
from app.services.guess_image_bank import save_question
from app.services.plugin_runtime.contracts import PluginExecutionContext, PluginExecutionRequest
from app.services.task_queue import parse_job_payload
from plugins.guess_image import plugin as game
from test_guess_image_bank import png


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    engine = create_engine(f"sqlite:///{tmp_path / 'game.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id=1, code="game", name="Game"))
        db.add(Bot(id=1, tenant_id=1, name="Bot", is_enabled=True))
        plugin_directory.sync_plugins_from_directory(db, force=True)
        plugin = db.scalar(select(Plugin).where(Plugin.code == game.CODE))
        db.add(BotPlugin(id=1, tenant_id=1, bot_id=1, plugin_id=plugin.id, status="enabled", group_default_enabled=True, config_json="{}"))
        db.add(Channel(id=1, tenant_id=1, bot_id=1, name="Test", channel_type="wechat_ipad", is_enabled=True))
        db.add(Conversation(id=1, tenant_id=1, bot_id=1, channel_id=1, channel_type="wechat_ipad", external_id="group-one", chat_type="group"))
        db.add(Chatroom(id=1, tenant_id=1, bot_id=1, channel_id=1, external_id="group-one", is_active=True,
                        management_status="enabled", status="synced", owner_external_id="owner"))
        db.add(ChatroomMember(tenant_id=1, chatroom_id=1, bot_id=1, channel_id=1, member_external_id="admin", role="admin", status="active"))
        db.commit()
    with tenant_scope(1), tenant_session(factory) as db:
        for index in range(7):
            save_question(db, {"answer": f"Answer{index}", "status": "enabled", "source": "example", "license": "CC0"}, png())
        db.commit()
        context = PluginExecutionContext(1, 1, 1, channel_id=1, conversation_id=1, sender_id="player", channel_type="wechat_ipad", chat_type="group", message_type="text")
        yield SimpleNamespace(db=db, factory=factory, context=context, config={"rounds": 2, "seconds": 60, "hint_remaining": 30, "daily_limit": 5})


def state(env):
    return game._state(env.db, env.context, game._key(1, "group-one"))


def send(env, content, user="player", **payload):
    message = Message(tenant_id=1, conversation_id=1, direction="in", sender_id=user, sender_name="测试玩家", content=content, message_type="text")
    env.db.add(message)
    env.db.flush()
    request = PluginExecutionRequest("execute", env.config, payload, replace(env.context, message_id=message.id, sender_id=user))
    result = game.execute_message_action(env.db, request)
    env.db.flush()
    return request, result


def timer(env, kind, value=None):
    value = value or state(env)["game"]
    return game.execute_scheduled_action(env.db, PluginExecutionRequest(f"gi.{kind}.{value['id'][:8]}.{value['round']}", env.config,
        {"game": value["id"], "round": value["round"], "kind": kind}, env.context))


def open_round(env):
    value = state(env)["game"]
    job = env.db.get(TaskJob, value["image_job"])
    message = Message(tenant_id=1, conversation_id=1, direction="out", sender_id="bot", content="image", message_type="image", status="sent", external_status="sent")
    env.db.add(message)
    env.db.flush()
    job.status = "completed"
    job.result_json = json.dumps({"message_id": message.id})
    env.db.flush()
    timer(env, "delivery")
    return state(env)["game"]


def test_manifest_and_generic_admin_metadata():
    found = next(item for item in plugin_directory.discover_plugins(force=True) if item.manifest.code == game.CODE)
    assert found.is_valid and found.admin_config["title"] == "管理题库"
    assert found.capability.consume_policy == "continue"


def test_start_reserves_once_and_uniform_sampling_has_no_repeats(env):
    request, _ = send(env, "猜图片")
    value = state(env)
    assert value["used"] == 1 and value["game"]["status"] == "sending"
    assert len({q["id"] for q in value["game"]["questions"]}) == 2
    game.execute_message_action(env.db, request)
    assert state(env)["used"] == 1
    send(env, "guess-image")
    assert state(env)["game"]["id"] == value["game"]["id"]
    assert state(env)["used"] == 1
    opened = open_round(env)
    assert opened["status"] == "open" and opened["deadline"] - opened["opened_at"] == 60


def test_exact_plain_text_only_and_duplicate_correct_cannot_score_next_round(env):
    send(env, "猜图片")
    value = open_round(env)
    answer = value["questions"][0]["answer"]
    for content, payload in ((answer.lower(), {}), (answer + " ", {}), (answer, {"attachments": [{"type": "image"}]}),
        (answer, {"normalized_event": {"details": {"quote_payload": {"title": answer}}}}),
        (answer, {"normalized_event": {"details": {"content_xml": "<msg/>"}}})):
        send(env, content, **payload)
        assert state(env)["game"]["round"] == 0
    request, _ = send(env, answer)
    assert state(env)["game"]["round"] == 1
    assert state(env)["game"]["scores"][0]["score"] == 1
    game.execute_message_action(env.db, request)
    assert state(env)["game"]["scores"][0]["score"] == 1
    assert timer(env, "timeout", value).output["provider_status"] == "stale_timer"
    open_round(env)
    send(env, state(env)["game"]["questions"][1]["answer"], user="second")
    assert state(env)["game"]["status"] == "finished"
    finish = env.db.scalars(select(TaskJob).where(TaskJob.dedupe_key.like("%:finish"))).one()
    text = parse_job_payload(finish)["payload"]["content"]
    assert "本局排行榜" in text and text.index("1.") < text.index("2.")


def test_send_failure_refunds_first_round_and_duplicate_timer_is_safe(env):
    send(env, "猜图片")
    value = state(env)["game"]
    env.db.get(TaskJob, value["image_job"]).status = "failed"
    env.db.flush()
    timer(env, "delivery")
    assert state(env)["used"] == 0 and state(env)["game"]["status"] == "finished"
    timer(env, "delivery", value)
    assert state(env)["used"] == 0


@pytest.mark.parametrize("kind", [None, "image", "text", "voice"])
def test_timeout_counts_any_human_activity_and_ends_when_quiet(env, monkeypatch, kind):
    send(env, "猜图片")
    value = open_round(env)
    if kind:
        env.db.add(Message(tenant_id=1, conversation_id=1, direction="in", sender_id="other", content="其他命令", message_type=kind,
                           created_at=datetime.fromtimestamp(value["opened_at"] + 1, now_shanghai().tzinfo)))
        env.db.flush()
    monkeypatch.setattr(game, "now_shanghai", lambda: datetime.fromtimestamp(value["deadline"] + 1, timezone(timedelta(hours=8))))
    timer(env, "timeout")
    current = state(env)["game"]
    assert current["status"] == ("sending" if kind else "finished")
    assert current["round"] == (1 if kind else 0)


def test_hint_once_each_round_restart_recovery_and_no_refund_after_started(env, monkeypatch):
    send(env, "猜图片")
    value = open_round(env)
    monkeypatch.setattr(game, "now_shanghai", lambda: datetime.fromtimestamp(value["deadline"] - 29, timezone(timedelta(hours=8))))
    timer(env, "hint")
    timer(env, "hint")
    assert len(env.db.scalars(select(TaskJob).where(TaskJob.dedupe_key.like("%:hint"))).all()) == 1
    env.db.commit()
    # Recovery reads database state, not an in-process timer or cache.
    with tenant_session(env.factory) as recovered:
        assert game._state(recovered, env.context, game._key(1, "group-one"))["game"]["hinted"] is True
    send(env, value["questions"][0]["answer"])
    assert state(env)["game"]["hinted"] is False
    next_game = state(env)["game"]
    env.db.get(TaskJob, next_game["image_job"]).status = "failed"
    env.db.flush()
    timer(env, "delivery")
    assert state(env)["used"] == 1


def test_limit_only_group_owner_admin_beijing_reset_and_unlimited(env, monkeypatch):
    send(env, "猜图片 次数 0")
    assert state(env).get("limit") is None
    send(env, "猜图片 次数 0", user="admin")
    assert state(env)["limit"] == 0
    send(env, "猜图片")
    assert state(env)["used"] == 0
    send(env, "猜图片次数 默认", user="owner")
    assert state(env)["limit"] is None
    value = {"day": "2026-11-12", "used": 5}
    monkeypatch.setattr(game, "now_shanghai", lambda: datetime(2026, 11, 12, 16, tzinfo=timezone.utc))
    game._day(value)
    assert value["day"] == "2026-11-13" and value["used"] == 0


def test_disabled_scope_cannot_start_or_answer(env):
    env.db.get(BotPlugin, 1).status = "disabled"
    env.db.flush()
    _, result = send(env, "猜图片")
    assert result.output["provider_status"] == "unavailable" and not state(env)


def test_concurrent_starts_and_correct_answers_have_one_winner(env):
    def concurrent(content):
        requests = []
        for user in ("first", "second"):
            message = Message(tenant_id=1, conversation_id=1, direction="in", sender_id=user, content=content, message_type="text")
            env.db.add(message)
            env.db.flush()
            requests.append(PluginExecutionRequest("execute", env.config, {}, replace(env.context, message_id=message.id, sender_id=user)))
        env.db.commit()
        barrier = Barrier(2)
        def execute(request):
            with tenant_scope(1), tenant_session(env.factory) as db:
                barrier.wait(timeout=10)
                result = game.execute_message_action(db, request)
                db.commit()
                return result.ok
        with ThreadPoolExecutor(2) as executor:
            assert all(executor.map(execute, requests))
    concurrent("猜图片")
    assert state(env)["used"] == 1
    value = open_round(env)
    concurrent(value["questions"][0]["answer"])
    value = state(env)["game"]
    assert value["round"] == 1 and sum(score["score"] for score in value["scores"]) == 1


def test_contentless_online_media_envelopes_count_as_activity(env):
    send(env, "猜图片")
    value = open_round(env)
    channel = env.db.get(Channel, 1)
    channel.channel_type = "wechat_webridge"
    env.db.add(RawWebhookEvent(tenant_id=1, channel_id=1, source="wechat_webridge", payload_json=json.dumps(
        {"body": {"chat_id": "group-one", "is_group": True, "sender_id": "other", "msg_type": "image"}}),
        created_at=datetime.fromtimestamp(value["opened_at"] + 1, now_shanghai().tzinfo)))
    env.db.flush()
    assert game._activity(env.db, env.db.get(Conversation, 1), channel, value)


def test_pending_phone_confirmation_does_not_open_round(env):
    send(env, "猜图片")
    value = state(env)["game"]
    job = env.db.get(TaskJob, value["image_job"])
    message = Message(tenant_id=1, conversation_id=1, direction="out", sender_id="bot", content="image", message_type="image", status="sent", external_status="pending")
    env.db.add(message)
    env.db.flush()
    job.status = "completed"
    job.result_json = json.dumps({"message_id": message.id})
    env.db.flush()
    timer(env, "delivery")
    assert state(env)["game"]["status"] == "sending"


def test_admin_extension_rejects_external_and_unsafe_urls():
    from dataclasses import replace as copy
    from app.services.plugin_runtime.registry import _derive_admin_extension
    found = next(item for item in plugin_directory.discover_plugins(force=True) if item.manifest.code == game.CODE)
    for path in ("https://example.org", "//example.org", "/plugin-admin/guess_image/../other", "/plugin-admin/guess_image/%2e%2e/other", "javascript:alert(1)"):
        assert not _derive_admin_extension(copy(found, admin_config={"enabled": True, "base_path": path}))["enabled"]


def test_raw_text_restores_spaces_trimmed_by_display_adapters():
    assert game._original_text({"message": {"content": " Apple "}}, "Apple", "person") == " Apple "
    assert game._original_text({"Data": {"Content": "person:\n Apple "}}, "Apple", "person") == " Apple "
    assert game._original_text({"Data": {"Content": {"string": " Apple "}}}, "Apple", "person") == " Apple "


def test_group_scope_revocation_stops_current_game(env):
    send(env, "猜图片")
    value = open_round(env)
    env.db.get(BotPlugin, 1).group_default_enabled = False
    env.db.flush()
    timer(env, "timeout", value)
    assert state(env)["game"]["status"] == "finished"
    assert state(env)["used"] == 1
