"""账号(渠道)逻辑删除:只能删未启用的账号,释放额度,从各处隐藏并拒绝入站,数据保留。"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="channel-delete-logs-"))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, Channel, Chatroom, Message, RawWebhookEvent
from app.models.saas import BotBinding, Tenant
from app.services import channel_events, crud
from app.services.channel_deletion import ChannelDeletionError, delete_channel_account
from app.services.directory_pages import build_chatroom_directory, channel_options


@pytest.fixture()
def env():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            db.add(Tenant(id=1, code="t", name="t"))
            bot = Bot(tenant_id=1, name="机器人", status="running", is_enabled=True)
            db.add(bot)
            db.flush()
            channel = Channel(
                tenant_id=1, bot_id=bot.id, name="小明", channel_type="wechat_webridge", status="offline", is_enabled=False,
                bot_token_encrypted="cipher", bot_token_masked="****",
            )
            db.add(channel)
            db.flush()
            db.add(Chatroom(tenant_id=1, bot_id=bot.id, channel_id=channel.id, external_id="g@chatroom", name="某群"))
            db.add(
                BotBinding(
                    tenant_id=1, bot_id=bot.id, channel_id=channel.id, adapter_type="wechat_webridge", bot_account_id="wxid_a",
                    device_fingerprint_hash="f" * 64, status="active",
                )
            )
            db.flush()
            yield db, factory, channel


def test_enabled_account_cannot_be_deleted(env) -> None:
    db, _, channel = env
    channel.is_enabled = True
    with pytest.raises(ChannelDeletionError):
        delete_channel_account(db, channel)
    assert channel.deleted_at is None


def test_delete_hides_account_releases_quota_and_keeps_data(env) -> None:
    db, _, channel = env
    assert [item["id"] for item in channel_options(db)] == [channel.id]
    assert build_chatroom_directory(db)["summary"]["total"] == 1

    assert delete_channel_account(db, channel) == 1
    binding = db.scalar(select(BotBinding))
    # 额度立即释放(只统计 active/disabled),绑定记录保留供审计
    assert binding.status == "released" and binding.released_at is not None and binding.release_reason
    # 账号逻辑删除:令牌清掉,其余数据保留
    assert channel.deleted_at is not None and channel.status == "deleted" and channel.bot_token_encrypted is None
    assert int(db.scalar(select(func.count()).select_from(Chatroom)) or 0) == 1
    # 各处隐藏
    assert crud.get_channel(db, channel.id) is None
    assert channel_options(db) == []
    assert build_chatroom_directory(db)["summary"]["total"] == 0 and build_chatroom_directory(db)["rows"] == []
    # 重复删除不再释放
    assert delete_channel_account(db, channel) == 0


def test_deleted_account_ignores_inbound_events(env) -> None:
    db, _, channel = env
    delete_channel_account(db, channel)
    event = {
        "event_key": "e1",
        "should_persist_message": True,
        "conversation": {"external_id": "g@chatroom", "chat_type": "group"},
        "message": {"message_type": "text", "content": "你好", "sender_id": "x"},
        "details": {},
    }
    result = channel_events.ingest_normalized_event(db, channel, event)
    assert result["ignored"] is True and result["reason"] == "channel_deleted"
    assert int(db.scalar(select(func.count()).select_from(RawWebhookEvent)) or 0) == 0
    assert int(db.scalar(select(func.count()).select_from(Message)) or 0) == 0


def test_webridge_phone_requests_rejected_for_deleted_account(env, monkeypatch) -> None:
    from app.api import wechat_webridge

    db, _, channel = env

    @contextmanager
    def fake_platform_session(_factory):
        yield db

    monkeypatch.setattr(wechat_webridge, "platform_session", fake_platform_session)
    assert wechat_webridge._channel_tenant(channel.id) == (1, channel.id)
    delete_channel_account(db, channel)
    with pytest.raises(HTTPException) as exc:
        wechat_webridge._channel_tenant(channel.id)
    assert exc.value.status_code == 404
