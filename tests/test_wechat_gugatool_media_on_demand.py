"""微信 iOS 按需取媒体的测试。

覆盖：点击媒体卡片后建附件并入队、附件已有定位信息时复用、其它渠道仍按不支持处理。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel, Conversation, Message, MessageAttachment, RawWebhookEvent, TaskJob
from app.services import message_media
from app.services.inbound_media import create_wechat_media_attachment
from app.services.task_queue import INBOUND_MEDIA_FETCH_JOB


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'media.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, *, channel_type: str = "wechat_gugatool", with_event: bool = True):
    channel = Channel(bot_id=1, name="iOS", channel_type=channel_type, api_base="http://127.0.0.1:19090", tenant_id=7)
    db.add(channel)
    db.flush()
    conversation = Conversation(
        bot_id=1,
        channel_id=channel.id,
        channel_type=channel_type,
        external_id="123@chatroom",
        title="群",
        chat_type="group",
        tenant_id=7,
    )
    db.add(conversation)
    db.flush()
    message = Message(
        conversation_id=conversation.id,
        direction="in",
        sender_id="wxid_a",
        sender_name="甲",
        content="[图片]",
        message_type="image",
        external_message_id="evt-1",
        status="received",
        tenant_id=7,
    )
    db.add(message)
    db.flush()
    if with_event:
        db.add(
            RawWebhookEvent(
                channel_id=channel.id,
                source="wechat_gugatool_agent",
                event_key="evt-1",
                payload_json=json.dumps({"localMessageID": 32673, "serverMessageID": 6332842306965274974, "fromUser": "123@chatroom", "messageType": 3}),
                tenant_id=7,
            )
        )
        db.flush()
    return channel, conversation, message


def test_request_creates_attachment_and_enqueues_job(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel, conversation, message = _seed(db)

        outcome = message_media.request(db, message)
        db.commit()

        assert outcome["state"] == "loading"
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == message.id))
        assert attachment.provider == "wechat_gugatool"
        assert attachment.attachment_type == "image"
        meta = json.loads(attachment.metadata_json)
        assert meta["to_wxid"] == "123@chatroom"
        assert meta["local_message_id"] == 32673
        assert meta["server_message_id"] == 6332842306965274974

        job = db.scalar(select(TaskJob).where(TaskJob.id == int(meta["upload_job_id"])))
        assert job.job_type == INBOUND_MEDIA_FETCH_JOB
        assert json.loads(job.payload_json)["attachment_id"] == attachment.id


def test_request_reuses_existing_attachment_metadata(session_factory) -> None:
    """自动下载留下的附件已经有定位信息时不再回查原始事件。"""

    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel, conversation, message = _seed(db, with_event=False)
        create_wechat_media_attachment(
            db,
            channel=channel,
            raw_event_id=None,
            conversation_id=conversation.id,
            message_id=message.id,
            attachment_type="image",
            metadata={"to_wxid": "123@chatroom", "local_message_id": 99, "server_message_id": 100},
            provider="wechat_gugatool",
        )
        db.flush()

        outcome = message_media.request(db, message)
        db.commit()

        assert outcome["state"] == "loading"
        attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.message_id == message.id))
        assert json.loads(attachment.metadata_json)["local_message_id"] == 99


def test_request_without_message_ids_reports_actionable_error(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        _channel, _conversation, message = _seed(db, with_event=False)

        outcome = message_media.request(db, message)

        assert outcome["state"] == "failed"
        assert "消息编号" in outcome["error"]


def test_other_channels_still_report_unsupported(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        _channel, _conversation, message = _seed(db, channel_type="telegram")

        outcome = message_media.request(db, message)

        assert outcome["state"] == "failed"
        assert "暂不支持按需获取媒体" in outcome["error"]
