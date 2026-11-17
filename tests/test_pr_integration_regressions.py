"""PR 审计缺陷回归：验证真实配置合并和有明确证据的消息撤回。"""

import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, Message, TaskJob
from app.services import message_actions, plugin_directory
from app.services.message_view import recall_info
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed


@pytest.mark.parametrize("provider", [None, "qqmusic", "netease"])
def test_music_provider_survives_real_dispatch_default_merge(provider):
    """覆盖运行默认、机器人绑定和群覆盖的真实合并链路，不能只测配置 loader。"""

    music = plugin_directory.load_directory_plugin("music", force=True).instance._module
    raw = {"api_base": "https://music.example.test"}
    if provider is not None:
        raw["provider"] = provider
    binding = BotPlugin(id=77, tenant_id=1, bot_id=1, plugin_id=77, config_json=json.dumps(raw))
    context = plugin_directory._build_directory_context(
        plugin_directory.PluginDispatchContext(tenant_id=1, bot_id=1, channel_id=1,
            channel_type="wechat_webridge", chat_type="group", message_type="text"),
        plugin_directory.PluginDispatchCandidate(binding_id=77, plugin_id=77, plugin_code="music",
            plugin_type="command", priority=0, consume_policy="continue", timeout_ms=30000,
            entrypoint_name="plugin.py", scope_config_override_json='{"result_limit": 3}'),
        binding, {}, raw_event_id=None, conversation_id=None, message_id=None,
    )
    config = music._load_config(context.binding_config)
    assert config.provider == (provider or "qqmusic")
    assert config.api_base == raw["api_base"] and config.result_limit == 3


@pytest.fixture()
def recall_context():
    """同一成员先后发出相同正文；事件编号与微信编号属于不同编号空间。"""

    db = _session()
    _seed(db)
    channel = db.get(Channel, 1)
    channel.channel_type = "wechat_gugatool"
    conversation = Conversation(tenant_id=1, bot_id=1, channel_id=1,
        channel_type="wechat_gugatool", external_id="recall@chatroom", chat_type="group")
    db.add(conversation)
    db.flush()
    rows = [Message(tenant_id=1, conversation_id=conversation.id, direction="in", sender_id="wxid_member",
        message_type="text", content="好的", external_message_id=f"event-{index}", status="received",
        raw_request_json=json.dumps({"native_ids": {"local": index, "server": server}}))
        for index, server in ((101, 18446744073709551000), (102, 18446744073709551001))]
    db.add_all(rows)
    db.commit()
    try:
        yield db, channel, conversation, rows
    finally:
        db.close()


def _recall(db, channel, conversation, xml):
    return message_actions.apply_gugatool_recall_event(db, channel, conversation, recall_info(xml),
        raw_event_id=999)


def test_recall_uses_exact_server_id_before_repeated_text(recall_context):
    db, channel, conversation, rows = recall_context
    changed = _recall(db, channel, conversation,
        '<sysmsg type="revokemsg"><revokemsg><newmsgid>18446744073709551000</newmsgid>'
        '<reEditStringContent>好的</reEditStringContent></revokemsg></sysmsg>')
    assert [row.id for row in changed] == [rows[0].id]
    assert [row.status for row in rows] == ["revoked", "received"]


@pytest.mark.parametrize("identifier", [
    "", "<newmsgid>99999</newmsgid>", "<msgid>18446744073709551000</msgid>",
    "<newmsgid>99999</newmsgid><msgid>101</msgid>",
    "<newmsgid>18446744073709551000</newmsgid><reEditReferMsgSvrId>18446744073709551001</reEditReferMsgSvrId>",
])
def test_recall_never_guesses_from_body_or_other_id_namespace(recall_context, identifier):
    db, channel, conversation, rows = recall_context
    changed = _recall(db, channel, conversation,
        f'<sysmsg type="revokemsg"><revokemsg>{identifier}'
        '<reEditStringContent>好的</reEditStringContent></revokemsg></sysmsg>')
    assert changed == []
    assert [row.status for row in rows] == ["received", "received"]


def test_recall_rejects_duplicate_native_identifiers(recall_context):
    db, channel, conversation, rows = recall_context
    rows[1].raw_request_json = rows[0].raw_request_json
    db.commit()
    assert _recall(db, channel, conversation,
        '<sysmsg type="revokemsg"><revokemsg><newmsgid>18446744073709551000</newmsgid></revokemsg></sysmsg>') == []
    assert [row.status for row in rows] == ["received", "received"]


def test_recall_accepts_explicit_local_id(recall_context):
    db, channel, conversation, rows = recall_context
    changed = _recall(db, channel, conversation,
        '<sysmsg type="revokemsg"><revokemsg><msgid>101</msgid></revokemsg></sysmsg>')
    assert [row.id for row in changed] == [rows[0].id]


@pytest.mark.parametrize("notice", ["你撤回了一条消息", "一位成员撤回了一条消息"])
def test_unknown_recall_is_saved_without_peer_identity_or_dispatch(recall_context, notice):
    """走完整入站链路：未知撤回只保存可读系统通知，既不误撤也不产生群资料或指令任务。"""

    from app.services.channel_events import ingest_inbound_event

    db, channel, conversation, _ = recall_context
    with tenant_scope(1), tenant_session(sessionmaker(bind=db.get_bind())) as scoped:
        result = ingest_inbound_event(scoped, scoped.get(Channel, channel.id), {
            "id": "unknown-recall", "kind": "incoming", "fromUser": conversation.external_id,
            "toUser": "wxid_bot", "messageType": 10002,
            "content": '<sysmsg type="revokemsgcontent"><revokemsgcontent>'
                f'<text>{notice}</text><link><reEditStringContent>好的</reEditStringContent></link>'
                '</revokemsgcontent></sysmsg>',
        })
        assert result["dispatch"]["mode"] == "storage_only" and result["dispatch"]["matched"] is False
        notification = scoped.get(Message, result["dispatch"]["message_id"])
        assert notification.message_type == "system" and notification.content == notice
        assert notification.sender_id is None and notification.sender_name is None
        assert scoped.scalar(select(func.count()).select_from(TaskJob)) == 0
        assert scoped.scalar(select(func.count()).select_from(Chatroom)) == 0
        assert all(row.status == "received" for row in scoped.scalars(
            select(Message).where(Message.message_type == "text")).all())
