"""设备拉取已排队消息时，仍须遵守刚刚保存的群插件权限。"""

import json

import pytest
from sqlalchemy import select

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import BotPlugin, Channel, Conversation, GroupPluginScope
from app.models.saas import ProviderNode, ProviderNodeAccount
from app.services.outbound_batch import enqueue_outbound_job
from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, enqueue_job
from app.services.wechat_webridge.service import claim_commands, enqueue_command
from app.services.workwechat_cloud_poll import claim_cloud_jobs, claim_provider_node_jobs
from test_group_gate_webridge import ROOM, env  # noqa: F401


@pytest.fixture(params=["webridge", "cloud_channel", "cloud_node"])
def claim_context(env, request, monkeypatch):
    factory, ids = env
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        channel = db.get(Channel, ids["channel"])
        if request.param != "webridge":
            channel.channel_type = "workwechat"
            channel.adapter_config_json = json.dumps({"workwechat_delivery_mode": "cloud_poll"})
        node_id = None
        if request.param == "cloud_node":
            node = ProviderNode(name="测试节点", status="active")
            db.add(node)
            db.flush()
            account = ProviderNodeAccount(provider_node_id=node.id, account_key="test-account", status="online")
            db.add(account)
            db.flush()
            channel.provider_node_id, channel.provider_account_id = node.id, account.id
            node_id = node.id
        conversation = Conversation(bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type, external_id=ROOM, chat_type="group")
        db.add(conversation)
        db.commit()
        if request.param == "webridge":
            # 账号鉴权和定时维护另有协议测试；本用例只观察消息队列的群权限边界。
            monkeypatch.setattr("app.services.wechat_webridge.service.identity_matches", lambda *_: True)
            monkeypatch.setattr("app.services.wechat_webridge.service._poll_maintenance", lambda *_: None)
            claim = lambda: claim_commands(db, channel, {}, limit=10)
        elif request.param == "cloud_channel":
            claim = lambda: claim_cloud_jobs(db, channel, batch_size=10)
        else:
            claim = lambda: claim_provider_node_jobs(db, node_id=node_id, batch_size=10)
        yield db, channel, conversation, ids, claim


def _enqueue(db, channel, conversation, ids, *, plugin_code="message_sign"):
    return enqueue_outbound_job(db, channel=channel, conversation=conversation, plugin_code=plugin_code,
                                payload={"receiver_id": ROOM, "content": "测试消息", "message_type": "text"},
                                related_type="message_sign_binding", related_id=ids["binding"], priority=70)


def test_claim_cancels_queued_plugin_message_after_group_disabled(claim_context):
    db, channel, conversation, ids, claim = claim_context
    job = _enqueue(db, channel, conversation, ids)
    db.add(GroupPluginScope(channel_id=channel.id, chatroom_id=ids["room"], bot_plugin_id=ids["binding"], enabled=False, scope_status="overridden"))
    db.commit()
    assert claim() == []
    assert job.status == "cancelled" and job.finished_at is not None and job.attempts == 0
    # 以后重新开放群权限，也不能恢复这条旧消息。
    db.scalar(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == ids["room"])).enabled = True
    db.commit()
    assert claim() == []


@pytest.mark.parametrize("status", ["disabled", "unbound"])
def test_claim_cancels_queued_plugin_message_when_binding_stops(claim_context, status):
    db, channel, conversation, ids, claim = claim_context
    job = _enqueue(db, channel, conversation, ids)
    db.get(BotPlugin, ids["binding"]).status = status
    db.commit()
    assert claim() == []
    assert job.status == "cancelled"


def test_claim_preserves_authorized_plugin_manual_and_framework_messages(claim_context):
    db, channel, conversation, ids, claim = claim_context
    db.get(BotPlugin, ids["binding"]).group_default_enabled = False
    db.add(GroupPluginScope(channel_id=channel.id, chatroom_id=ids["room"], bot_plugin_id=ids["binding"], enabled=True, scope_status="overridden"))
    db.commit()
    jobs = [_enqueue(db, channel, conversation, ids, plugin_code=code) for code in ["message_sign", "", "ai_dispatch", "group_feature:welcome"]]
    if channel.channel_type == "wechat_webridge":
        control = enqueue_command(db, channel, command_name="account_status", args={})
    else:
        control = enqueue_job(db, job_type=WORKWECHAT_CLOUD_CONTROL_JOB, payload={"channel_id": channel.id, "action": "sync_chatrooms", "payload": {}}, related_type="channel", related_id=channel.id)
    db.commit()
    assert len(claim()) == 5
    assert all(job.status == "running" for job in [*jobs, control])


def test_claim_does_not_send_plugin_message_to_mismatched_conversation(claim_context):
    db, channel, conversation, ids, claim = claim_context
    job = _enqueue(db, channel, conversation, ids)
    other_channel = Channel(bot_id=channel.bot_id, name="其他账号", channel_type=channel.channel_type)
    db.add(other_channel)
    db.flush()
    conversation.channel_id = other_channel.id
    db.commit()
    assert claim() == []
    assert job.status == "cancelled"
