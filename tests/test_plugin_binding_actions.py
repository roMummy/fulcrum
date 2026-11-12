"""插件软解绑和首次绑定群范围，使用真实认证及租户隔离验证。"""

import json

import pytest
from sqlalchemy import select

from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, PluginStateRecord, TaskJob
from app.services.async_workers import _process_outbound_dispatch_job
from app.services.task_queue import OUTBOUND_DISPATCH_JOB, PLUGIN_SCHEDULED_ACTION_JOB
from test_plugin_detail_page import pages  # noqa: F401
from test_provider_node_pages import _login, provider_page_context  # noqa: F401


def test_unbind_preserves_history_and_cancels_only_this_binding_jobs(pages):
    client, ids, plugin_id, binding_id = pages
    with ids["factory"]() as db:
        channel = db.scalar(select(Channel).where(Channel.bot_id == ids["bot"]))
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, external_id="test@chatroom")
        db.add(room)
        db.flush()
        db.add(GroupPluginScope(tenant_id=ids["alpha"], channel_id=channel.id, chatroom_id=room.id, bot_plugin_id=binding_id, enabled=True))
        db.add(PluginStateRecord(tenant_id=ids["alpha"], bot_plugin_id=binding_id, namespace="test", state_key="history", payload_json='{"score":12}'))
        payload = json.dumps({"plugin_code": "weather", "channel_id": channel.id})
        jobs = [
            TaskJob(tenant_id=ids["alpha"], job_type=PLUGIN_SCHEDULED_ACTION_JOB, related_type="plugin_action:daily", related_id=binding_id, payload_json="{}"),
            TaskJob(tenant_id=ids["alpha"], job_type=OUTBOUND_DISPATCH_JOB, related_type="weather_binding", related_id=binding_id, payload_json="{}"),
            TaskJob(tenant_id=ids["alpha"], job_type=OUTBOUND_DISPATCH_JOB, related_type="weather_subscription", related_id=999, payload_json=payload),
            TaskJob(tenant_id=ids["alpha"], job_type=OUTBOUND_DISPATCH_JOB, related_type="conversation", related_id=1, payload_json=json.dumps({"plugin_code": "dota2_stats", "channel_id": channel.id})),
        ]
        db.add_all(jobs)
        db.commit()
        job_ids = [job.id for job in jobs]
    csrf = _login(client, "admin")
    response = client.post(f"/plugins/{binding_id}/unbind", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, binding_id)
        assert binding.status == "unbound" and binding.config_json == "{}"
        assert not binding.group_default_enabled
        assert db.scalar(select(PluginStateRecord).where(PluginStateRecord.bot_plugin_id == binding_id)) is not None
        assert db.scalar(select(GroupPluginScope).where(GroupPluginScope.bot_plugin_id == binding_id)) is None
        assert [db.get(TaskJob, value).status for value in job_ids] == ["cancelled", "cancelled", "cancelled", "pending"]
    html = client.get(f"/plugins/{plugin_id}").text
    assert f'data-gf-key="plugin-binding-{binding_id}"' not in html
    assert client.post(f"/plugins/{binding_id}/toggle", data={"csrf_token": csrf, "enabled": "true"}).status_code == 404


def test_unbind_checks_csrf_role_and_tenant(pages):
    client, ids, plugin_id, binding_id = pages
    _login(client, "admin")
    assert client.post(f"/plugins/{binding_id}/unbind").status_code == 403
    csrf = _login(client, "readonly")
    assert client.post(f"/plugins/{binding_id}/unbind", data={"csrf_token": csrf}).status_code == 403
    with ids["factory"]() as db:
        bot = Bot(tenant_id=ids["beta"], name="其他租户机器人")
        db.add(bot)
        db.flush()
        other = BotPlugin(tenant_id=ids["beta"], bot_id=bot.id, plugin_id=plugin_id)
        db.add(other)
        db.commit()
        other_id = other.id
    csrf = _login(client, "admin")
    assert client.post(f"/plugins/{other_id}/unbind", data={"csrf_token": csrf}).status_code == 404


@pytest.mark.parametrize("choice,expected", [(None, False), ("false", False), ("true", True)])
def test_first_bind_defaults_closed_unless_explicitly_opened(pages, choice, expected):
    client, ids, plugin_id, _binding_id = pages
    with ids["factory"]() as db:
        bot = Bot(tenant_id=ids["alpha"], name="首次绑定机器人")
        db.add(bot)
        db.commit()
        bot_id = bot.id
    csrf = _login(client, "admin")
    data = {"csrf_token": csrf, "bot_id": bot_id, "plugin_id": plugin_id}
    if choice is not None:
        data["group_default_enabled"] = choice
    assert client.post("/plugins/bind", data=data, follow_redirects=False).status_code == 303
    with ids["factory"]() as db:
        binding = db.scalar(select(BotPlugin).where(BotPlugin.bot_id == bot_id, BotPlugin.plugin_id == plugin_id))
        assert binding.group_default_enabled is expected


def test_saving_existing_binding_preserves_scope_and_rebind_asks_again(pages):
    client, ids, plugin_id, binding_id = pages
    csrf = _login(client, "admin")
    data = {"csrf_token": csrf, "bot_id": ids["bot"], "plugin_id": plugin_id, "group_default_enabled": "false"}
    assert client.post("/plugins/bind", data=data, follow_redirects=False).status_code == 303
    with ids["factory"]() as db:
        assert db.get(BotPlugin, binding_id).group_default_enabled is True
    assert client.post(f"/plugins/{binding_id}/unbind", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    result = client.post("/plugins/toggle-direct", data={**data, "enabled": "true"})
    assert result.status_code == 200 and result.json()["binding_id"] == binding_id
    with ids["factory"]() as db:
        assert db.get(BotPlugin, binding_id).group_default_enabled is False


def test_outbound_worker_rechecks_scope_before_dispatch(pages, monkeypatch):
    _client, ids, _plugin_id, binding_id = pages
    import app.services.async_workers as worker
    def unexpected_send(*args, **kwargs):
        pytest.fail("未开放的群不应发送消息")
    monkeypatch.setattr(worker, "dispatch_outbound_message", unexpected_send)
    with ids["factory"]() as db:
        binding = db.get(BotPlugin, binding_id)
        binding.status = "enabled"
        binding.group_default_enabled = False
        channel = db.scalar(select(Channel).where(Channel.bot_id == ids["bot"]))
        conversation = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, channel_type=channel.channel_type, chat_type="group", external_id="new@chatroom")
        db.add(conversation)
        db.commit()
        from app.core.tenant_context import tenant_scope
        with tenant_scope(ids["alpha"]):
            result = _process_outbound_dispatch_job(db, TaskJob(), {"channel_id": channel.id, "conversation_id": conversation.id, "plugin_code": "weather", "payload": {"content": "测试"}})
        assert result == {"skipped": True, "reason": "plugin_scope_disabled"}


def test_detail_exposes_confirmation_and_explicit_first_binding_choice(pages):
    client, _ids, plugin_id, binding_id = pages
    _login(client, "admin")
    html = client.get(f"/plugins/{plugin_id}").text
    assert f'action="/plugins/{binding_id}/unbind"' in html and 'data-confirm-title="解绑机器人？"' in html
    assert 'name="group_default_enabled" required' in html and "首次绑定后，是否对所有群开放" in html
    assert 'data-plugin-binding="new"' in client.get("/plugins").text


@pytest.mark.parametrize("plugin_code", ["ai_dispatch", "group_feature:welcome"])
def test_framework_replies_do_not_require_a_directory_plugin_binding(pages, monkeypatch, plugin_code):
    _client, ids, _plugin_id, _binding_id = pages
    from types import SimpleNamespace
    import app.services.async_workers as worker
    monkeypatch.setattr(worker, "dispatch_outbound_message", lambda *args: (SimpleNamespace(id=123), {"ok": True}))
    with ids["factory"]() as db:
        channel = db.scalar(select(Channel).where(Channel.bot_id == ids["bot"]))
        channel.channel_type = "wechat_webridge"
        conversation = Conversation(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel.id, channel_type=channel.channel_type, chat_type="group", external_id="core@chatroom")
        db.add(conversation)
        db.commit()
        result = _process_outbound_dispatch_job(db, TaskJob(), {"channel_id": channel.id, "conversation_id": conversation.id, "plugin_code": plugin_code, "payload": {"content": "测试"}})
        assert result["message_id"] == 123
