"""网页发送与手机回显必须复用同一条消息，不能吞掉真实的重复发送。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import timedelta
from html import escape
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-echo-logs-"))

import pytest
from sqlalchemy import func, select

from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, Conversation, Message, MessageAttachment, MessageActivityHourly, RawWebhookEvent, TaskJob
from app.services.channel_adapters.service import dispatch_outbound_message
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.channel_events import ingest_normalized_event
from app.services.wechat_webridge import service
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed


OWN = "wxid_echo_test_bot"


@pytest.fixture()
def echo_env():
    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": {"account_id": OWN}})
    conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id,
                                channel_type="wechat_webridge", external_id="echo-test@chatroom", chat_type="group")
    db.add(conversation)
    db.commit()
    yield db, channel, conversation
    db.close()


def _send(db, channel, conversation, *, kind="text", content="测试文字", status="running", **fields):
    message, outcome = dispatch_outbound_message(db, channel, conversation,
                                                  {"message_type": kind, "content": content, **fields})
    assert outcome["ok"] is True
    job = db.get(TaskJob, json.loads(message.raw_request_json)["job_id"])
    job.status = status
    job.started_at = now_shanghai() - timedelta(seconds=1)
    if status == "completed":
        service.settle_outbound_message(db, job, ok=True, result={"receiver": conversation.external_id})
        job.finished_at = now_shanghai()
    db.commit()
    return message, job


def _event(channel, conversation, *, kind="text", content="测试文字", native_id="native-1", sender=OWN):
    return WechatWeBridgeAdapter().normalize_inbound_payload(channel, {
        "event_id": "event-" + native_id, "event_name": "message",
        "body": {"chat_id": conversation.external_id, "room_id": conversation.external_id,
                 "sender_id": sender, "sender_name": "测试机器人", "msg_type": kind,
                 "content": content, "message_id": native_id, "is_group": True},
    })


def _reconcile(db, channel, conversation, event, *, received_at=None):
    from app.services.wechat_webridge.message_echo import reconcile_echo

    return reconcile_echo(db, channel, conversation, event, received_at=received_at or now_shanghai())


@pytest.mark.parametrize("completed_first", [False, True], ids=["event-before-result", "result-before-event"])
def test_echo_reuses_original_in_both_delivery_orders(echo_env, completed_first):
    db, channel, conversation = echo_env
    original, job = _send(db, channel, conversation, status="completed" if completed_first else "running")
    result = _reconcile(db, channel, conversation, _event(channel, conversation))
    assert result is not None and result.id == original.id
    if not completed_first:
        service.settle_outbound_message(db, job, ok=True, result={"receiver": conversation.external_id})
    db.commit()
    assert db.scalar(select(func.count(Message.id))) == 1
    assert original.external_message_id == "native-1" and original.sender_id == OWN
    assert original.content == "测试文字" and original.status == "sent"
    assert json.loads(original.raw_request_json).get("echo")


def test_two_real_sends_with_identical_text_keep_two_distinct_records(echo_env):
    db, channel, conversation = echo_env
    first, _ = _send(db, channel, conversation)
    second, _ = _send(db, channel, conversation)
    matches = [_reconcile(db, channel, conversation, _event(channel, conversation, native_id=native))
               for native in ("native-1", "native-2")]
    assert all(message is not None for message in matches)
    assert {message.id for message in matches} == {first.id, second.id}
    assert {first.external_message_id, second.external_message_id} == {"native-1", "native-2"}
    assert db.scalar(select(func.count(Message.id))) == 2
    # 同一账号手机上再手动发相同文字，没有尚未认领的网页发送，必须作为新消息保留。
    assert _reconcile(db, channel, conversation, _event(channel, conversation, native_id="manual-3")) is None


def test_manual_other_people_and_expired_events_do_not_match(echo_env):
    db, channel, conversation = echo_env
    assert _reconcile(db, channel, conversation, _event(channel, conversation)) is None
    original, job = _send(db, channel, conversation, status="completed")
    assert _reconcile(db, channel, conversation, _event(channel, conversation, sender="wxid_friend")) is None
    assert _reconcile(db, channel, conversation, _event(channel, conversation, content="手机独立发送")) is None
    assert _reconcile(db, channel, conversation, _event(channel, conversation),
                      received_at=now_shanghai() + timedelta(seconds=160)) is None
    assert original.external_message_id is None


def test_echo_is_scoped_to_conversation_and_channel(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation)
    other = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id,
                         channel_type="wechat_webridge", external_id="other@chatroom", chat_type="group")
    other_channel = Channel(tenant_id=1, bot_id=1, name="另一渠道", channel_type="wechat_webridge",
                            runtime_info_json=channel.runtime_info_json)
    db.add_all([other, other_channel])
    db.commit()
    assert _reconcile(db, channel, other, _event(channel, other)) is None
    assert _reconcile(db, other_channel, conversation, _event(other_channel, conversation)) is None
    assert original.external_message_id is None


def test_native_message_id_is_authoritative_even_after_execution_window(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, status="completed")
    original.external_message_id = "native-known"
    db.commit()
    result = _reconcile(db, channel, conversation,
                        _event(channel, conversation, content="渠道转换后的正文", native_id="native-known"),
                        received_at=now_shanghai() + timedelta(minutes=10))
    assert result is not None and result.id == original.id
    assert original.content == "测试文字"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_unexecuted_or_failed_commands_do_not_claim_phone_messages(echo_env, status):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, status=status)
    assert _reconcile(db, channel, conversation, _event(channel, conversation)) is None
    assert original.external_message_id is None


def test_file_echo_preserves_ready_local_attachment(echo_env, tmp_path):
    db, channel, conversation = echo_env
    data = b"test spreadsheet bytes"
    original, _ = _send(db, channel, conversation, kind="file", content="",
                         file_url="https://files.test/original.xlsx", file_name="表格.xlsx")
    path = tmp_path / "表格.xlsx"
    path.write_bytes(data)
    attachment = MessageAttachment(tenant_id=1, message_id=original.id, channel_id=channel.id,
                                    conversation_id=conversation.id, direction="out", provider="wechat_webridge",
                                    attachment_type="file", status="downloaded", storage_path=str(path), file_size=len(data),
                                    metadata_json=json.dumps({"file_name": path.name, "file_size": len(data)}))
    db.add(attachment)
    db.commit()
    xml = (f'<msg><appmsg><title>表格.xlsx</title><type>6</type><appattach><totallen>{len(data)}</totallen>'
           f'<fileext>xlsx</fileext><attachmd5>{hashlib.md5(data).hexdigest()}</attachmd5></appattach></appmsg></msg>')
    event = _event(channel, conversation, kind="app", content=OWN + ": " + xml)
    event["message"]["message_type"] = "file"
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id
    assert original.content == "[file]"
    assert db.scalar(select(func.count(MessageAttachment.id))) == 1
    assert attachment.message_id == original.id and attachment.status == "downloaded"
    assert path.read_bytes() == data


def test_same_file_name_and_size_with_different_md5_are_not_merged(echo_env, tmp_path):
    db, channel, conversation = echo_env
    data = b"original bytes"
    different = b"different data"
    assert len(data) == len(different)
    original, _ = _send(db, channel, conversation, kind="file", content="",
                         file_url="https://files.test/test.xlsx", file_name="相同文件名.xlsx")
    path = tmp_path / "相同文件名.xlsx"
    path.write_bytes(data)
    db.add(MessageAttachment(tenant_id=1, message_id=original.id, channel_id=channel.id,
                             conversation_id=conversation.id, direction="out", provider="wechat_webridge",
                             attachment_type="file", status="downloaded", storage_path=str(path), file_size=len(data),
                             metadata_json=json.dumps({"file_name": path.name, "file_size": len(data)})))
    db.commit()
    xml = (f'<msg><appmsg><title>{path.name}</title><type>6</type><appattach><totallen>{len(data)}</totallen>'
           f'<attachmd5>{hashlib.md5(different).hexdigest()}</attachmd5></appattach></appmsg></msg>')
    event = _event(channel, conversation, kind="app", content=OWN + ": " + xml)
    event["message"]["message_type"] = "file"
    assert _reconcile(db, channel, conversation, event) is None
    assert original.external_message_id is None and path.read_bytes() == data


def test_file_md5_cannot_be_verified_without_local_file(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, kind="file", content="",
                         file_url="https://files.test/test.xlsx", file_name="相同文件名.xlsx")
    db.add(MessageAttachment(tenant_id=1, message_id=original.id, channel_id=channel.id,
                             conversation_id=conversation.id, direction="out", provider="wechat_webridge",
                             attachment_type="file", status="pending", storage_path=None, file_size=14,
                             metadata_json=json.dumps({"file_name": "相同文件名.xlsx", "file_size": 14})))
    db.commit()
    xml = ('<msg><appmsg><title>相同文件名.xlsx</title><type>6</type><appattach><totallen>14</totallen>'
           '<attachmd5>9f79f3cf7d22c8f3dc91cb76f0c2ba0a</attachmd5></appattach></appmsg></msg>')
    event = _event(channel, conversation, kind="app", content=OWN + ": " + xml)
    event["message"]["message_type"] = "file"
    assert _reconcile(db, channel, conversation, event) is None
    assert original.external_message_id is None


def test_manual_message_six_seconds_after_success_does_not_claim_original(echo_env):
    db, channel, conversation = echo_env
    original, job = _send(db, channel, conversation, status="completed")
    received = job.finished_at + timedelta(seconds=6)
    assert _reconcile(db, channel, conversation, _event(channel, conversation), received_at=received) is None
    assert original.external_message_id is None


@pytest.mark.parametrize("milliseconds", [False, True], ids=["seconds", "milliseconds"])
def test_backlogged_event_creation_time_cannot_claim_new_command(echo_env, milliseconds):
    db, channel, conversation = echo_env
    original, job = _send(db, channel, conversation)
    event = _event(channel, conversation)
    timestamp = (job.started_at - timedelta(seconds=60)).timestamp()
    event["details"]["create_time"] = int(timestamp * (1000 if milliseconds else 1))
    assert _reconcile(db, channel, conversation, event) is None
    assert original.external_message_id is None


def test_phone_creation_time_tolerates_rounding_and_small_clock_skew(echo_env):
    db, channel, conversation = echo_env
    original, job = _send(db, channel, conversation)
    event = _event(channel, conversation)
    event["details"]["create_time"] = int((job.started_at - timedelta(seconds=4)).timestamp())
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id


def test_running_text_command_older_than_three_minutes_is_not_claimed(echo_env):
    db, channel, conversation = echo_env
    original, job = _send(db, channel, conversation)
    assert _reconcile(db, channel, conversation, _event(channel, conversation),
                      received_at=job.started_at + timedelta(seconds=181)) is None
    assert original.external_message_id is None


def test_running_media_command_older_than_five_seconds_is_not_claimed(echo_env):
    db, channel, conversation = echo_env
    original, job = _send(db, channel, conversation, kind="image", content="", image_url="https://files.test/1.jpg")
    event = _event(channel, conversation, kind="image", content='<msg><img length="123"/></msg>')
    assert _reconcile(db, channel, conversation, event,
                      received_at=job.started_at + timedelta(seconds=6)) is None
    assert original.external_message_id is None


def test_quote_echo_compares_reply_text_without_changing_local_content(echo_env):
    db, channel, conversation = echo_env
    text = "这是一条回复\n保留换行"
    original, _ = _send(db, channel, conversation, content=text, quote_message_id="quoted-original")
    xml = (f'<msg><appmsg><title>{escape(text)}</title><type>57</type><refermsg><type>1</type>'
           '<svrid>quoted-original</svrid><content>被引用文字</content></refermsg></appmsg></msg>')
    event = _event(channel, conversation, kind="app", content=OWN + ": " + xml)
    event["message"]["message_type"] = "quote"
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id
    assert original.content == text


def _reference(db, conversation, *, external_id="3843", sender="wxid_friend", content="被引用文字\n保留换行"):
    reference = Message(tenant_id=1, conversation_id=conversation.id, direction="in", sender_id=sender,
                        sender_name="小明", content=content, message_type="text", external_message_id=external_id)
    db.add(reference)
    db.commit()
    return reference


def _quote_event(channel, conversation, *, sender="wxid_friend", quoted_content="被引用文字\n保留换行"):
    xml = ('<msg><appmsg><title>这是引用回复\n保留换行</title><type>57</type><refermsg><type>1</type>'
           f'<svrid>8102491994130872356</svrid><fromusr>{escape(sender)}</fromusr>'
           f'<content>{escape(quoted_content)}</content></refermsg></appmsg></msg>')
    event = _event(channel, conversation, kind="app", content=OWN + ": " + xml)
    event["message"]["message_type"] = "quote"
    return event


def test_quote_with_phone_local_id_and_server_id_reuses_original(echo_env):
    db, channel, conversation = echo_env
    reference = _reference(db, conversation)
    original, _ = _send(db, channel, conversation, content="这是引用回复\n保留换行",
                         quote_message_id=reference.external_message_id)
    result = _reconcile(db, channel, conversation, _quote_event(channel, conversation))
    assert result is not None and result.id == original.id
    assert original.content == "这是引用回复\n保留换行" and original.external_message_id == "native-1"


@pytest.mark.parametrize("wrong_event", [False, True], ids=["linked-rendered-mention", "unrelated-event"])
def test_quote_matches_rendered_mention_only_from_the_linked_original_echo(echo_env, wrong_event):
    db, channel, conversation = echo_env
    reference = _reference(db, conversation, sender=OWN, content="原始正文")
    rendered = "@小明 原始正文"
    reference.raw_request_json = json.dumps({"echo": {"event_key": "linked-reference-event"}})
    db.add(RawWebhookEvent(tenant_id=1, channel_id=channel.id, source="wechat_webridge",
                           event_key="linked-reference-event", status="mapped", payload_json=json.dumps({
                               "event_name": "message", "body": {"chat_id": "wrong@chatroom" if wrong_event else conversation.external_id,
                                   "sender_id": OWN, "message_id": reference.external_message_id, "content": rendered}})))
    db.commit()
    original, _ = _send(db, channel, conversation, content="这是引用回复\n保留换行",
                         quote_message_id=reference.external_message_id)
    result = _reconcile(db, channel, conversation, _quote_event(channel, conversation, sender=OWN, quoted_content=rendered))
    if wrong_event:
        assert result is None and original.external_message_id is None
    else:
        assert result is not None and result.id == original.id
        assert reference.content == "原始正文" and original.external_message_id == "native-1"


@pytest.mark.parametrize("fields", [{"sender": "wxid_other"}, {"quoted_content": "不同的被引用文字"}],
                         ids=["wrong-sender", "wrong-content"])
def test_quote_different_ids_require_original_sender_and_content(echo_env, fields):
    db, channel, conversation = echo_env
    reference = _reference(db, conversation)
    original, _ = _send(db, channel, conversation, content="这是引用回复\n保留换行",
                         quote_message_id=reference.external_message_id)
    assert _reconcile(db, channel, conversation, _quote_event(channel, conversation, **fields)) is None
    assert original.external_message_id is None


def test_quote_different_ids_do_not_match_ambiguous_identical_references(echo_env):
    db, channel, conversation = echo_env
    reference = _reference(db, conversation)
    _reference(db, conversation, external_id="3844")
    original, _ = _send(db, channel, conversation, content="这是引用回复\n保留换行",
                         quote_message_id=reference.external_message_id)
    assert _reconcile(db, channel, conversation, _quote_event(channel, conversation)) is None
    assert original.external_message_id is None


def test_quote_different_ids_ignore_identical_reference_from_another_conversation(echo_env):
    db, channel, conversation = echo_env
    reference = _reference(db, conversation)
    other = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id,
                         channel_type="wechat_webridge", external_id="another@chatroom", chat_type="group")
    db.add(other)
    db.commit()
    _reference(db, other, external_id="3844")
    original, _ = _send(db, channel, conversation, content="这是引用回复\n保留换行",
                         quote_message_id=reference.external_message_id)
    result = _reconcile(db, channel, conversation, _quote_event(channel, conversation))
    assert result is not None and result.id == original.id


def test_mention_echo_matches_member_list_and_rendered_text(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, content="请看一下", mention_user_list=["wxid_friend"])
    event = _event(channel, conversation, content="@小明\u2005请看一下")
    event["details"]["mention_user_list"] = ["wxid_friend"]
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id
    assert original.content == "请看一下"


@pytest.mark.parametrize("different", [False, True], ids=["same-inline-body", "different-inline-body"])
def test_inline_mention_echo_keeps_readable_local_content(echo_env, different):
    db, channel, conversation = echo_env
    visible = "请 @小明\u2005看一下"
    original, _ = _send(db, channel, conversation, content="请 {{mention:wxid_friend}}看一下",
                         display_content=visible, mention_user_list=["wxid_friend"])
    event = _event(channel, conversation, content="请 @手机昵称\u2005" + ("不要看一下" if different else "看一下"))
    event["details"]["mention_user_list"] = ["wxid_friend"]
    result = _reconcile(db, channel, conversation, event)
    assert original.content == visible
    if different:
        assert result is None and original.external_message_id is None
    else:
        assert result is not None and result.id == original.id


def test_all_mention_echo_uses_native_notification_without_invented_name_prefix(echo_env):
    db, channel, conversation = echo_env
    db.add(Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id,
                    external_id=conversation.external_id, name="测试群", owner_external_id=OWN))
    db.commit()
    original, _ = _send(db, channel, conversation, content="@所有人\u2005开会",
                         mention_user_list=["notify@all"])
    event = _event(channel, conversation, content="@所有人\u2005开会")
    event["details"]["mention_user_list"] = ["notify@all"]
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id


def test_inline_mention_and_quote_reply_share_the_original_card(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, content="请{{mention:wxid_friend}}看一下",
                         display_content="请@本地昵称\u2005看一下", quote_message_id="quoted-original",
                         mention_user_list=["wxid_friend"])
    xml = ('<msg><appmsg><title>请@手机昵称\u2005看一下</title><type>57</type><refermsg>'
           '<type>1</type><svrid>quoted-original</svrid><content>原文</content></refermsg></appmsg></msg>')
    event = _event(channel, conversation, kind="app", content=OWN + ": " + xml)
    event["message"]["message_type"] = "quote"
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id
    assert original.content == "请@本地昵称\u2005看一下"


@pytest.mark.parametrize("content", ["第一行\n第二行", "@小明\u2005@小红\u2005第一行\n第二行"],
                         ids=["unmodified-text", "rendered-multiple-members"])
def test_mention_multiple_members_preserves_body_newlines(echo_env, content):
    db, channel, conversation = echo_env
    members = ["wxid_friend", "wxid_another_friend"]
    original, _ = _send(db, channel, conversation, content="第一行\n第二行", mention_user_list=members)
    event = _event(channel, conversation, content=content)
    event["details"]["mention_user_list"] = list(reversed(members))
    result = _reconcile(db, channel, conversation, event)
    assert result is not None and result.id == original.id
    assert original.content == "第一行\n第二行"


@pytest.mark.parametrize("content,members", [
    ("@小明\u2005请看一下", ["wxid_other"]),
    ("@小明\u2005@多余的人\u2005请看一下", ["wxid_friend"]),
], ids=["wrong-member-list", "extra-mention-prefix"])
def test_mention_requires_same_member_list_and_exact_prefix_count(echo_env, content, members):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, content="请看一下", mention_user_list=["wxid_friend"])
    event = _event(channel, conversation, content=content)
    event["details"]["mention_user_list"] = members
    assert _reconcile(db, channel, conversation, event) is None
    assert original.external_message_id is None


def test_mention_with_different_body_does_not_claim_original(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation, content="请看一下", mention_user_list=["wxid_friend"])
    event = _event(channel, conversation, content="@小明\u2005不要请看一下")
    event["details"]["mention_user_list"] = ["wxid_friend"]
    assert _reconcile(db, channel, conversation, event) is None
    assert original.external_message_id is None


def test_ambiguous_media_commands_are_not_claimed_by_type_alone(echo_env):
    db, channel, conversation = echo_env
    _send(db, channel, conversation, kind="image", content="", image_url="https://files.test/1.jpg")
    _send(db, channel, conversation, kind="image", content="", image_url="https://files.test/2.jpg")
    assert _reconcile(db, channel, conversation,
                      _event(channel, conversation, kind="image", content='<msg><img length="123"/></msg>')) is None


def test_ingestion_reuses_original_without_duplicate_stats_or_plugin_dispatch(echo_env):
    db, channel, conversation = echo_env
    original, _ = _send(db, channel, conversation)
    event = _event(channel, conversation)
    with patch("app.services.wechat_webridge.group_sync.observe_inbound_event"):
        result = ingest_normalized_event(db, channel, event, source="wechat_webridge")
    assert result["ok"] and result["mapped"]
    assert db.scalar(select(func.count(Message.id))) == 1
    assert db.get(Message, original.id).external_message_id == "native-1"
    assert db.scalar(select(func.sum(MessageActivityHourly.message_count))) == 1
    assert result["dispatch"]["queued"] is False
