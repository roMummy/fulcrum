"""看图猜成语(内置插件):排行榜显示本群群昵称而不是 wxid。"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="idiom-logs-"))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, PluginStateRecord
from app.services import plugin_directory


@pytest.fixture()
def env():
    plugin_directory._SCAN_CACHE.clear()
    engine = create_engine("sqlite://")
    for table in (Channel, Conversation, Chatroom, ChatroomMember, PluginStateRecord):
        table.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge")
            db.add(channel)
            db.flush()
            conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group")
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="群", is_active=True)
            db.add_all([conversation, room])
            db.flush()
            db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="wxid_a1", display_name="小明", status="active"))
            db.flush()
            yield db, channel, conversation


def test_the_bundled_plugin_is_valid_and_has_the_fixed_version() -> None:
    found = {item.manifest.code: item for item in plugin_directory.discover_plugins(force=True)}
    assert found["idiom_guess"].is_valid and found["idiom_guess"].manifest.version == "1.0.3"


def test_leaderboard_shows_group_nicknames_and_keeps_old_records_readable(env) -> None:
    db, channel, conversation = env
    module = plugin_directory.load_directory_plugin("idiom_guess", force=True).instance._module
    namespace = f"idiom_scores_{channel.id}_{module._room_key(channel.id, conversation.external_id).split(':')[-1]}"

    def put(key: str, **score) -> None:
        db.add(PluginStateRecord(tenant_id=1, bot_plugin_id=9, namespace=namespace, state_key=key, payload_json=json.dumps(score)))

    put("k1", count=5, name="wxid_a1", user_id="wxid_a1", last_correct_at=1.0)   # 当时记成了 wxid,成员表里有昵称
    put("k2", count=3, name="老朋友", last_correct_at=2.0)                         # 老记录:没有 user_id
    put("k3", count=2, name="wxid_zz", user_id="wxid_zz", last_correct_at=3.0)    # 成员表里没有:只能显示 ID
    db.flush()
    context = SimpleNamespace(tenant_id=1, binding_id=9, channel_id=channel.id)
    text = module._leaderboard(db, context, conversation, 10)
    assert text.splitlines() == ["【本群猜成语排行榜】", "1. 小明 - 5题", "2. 老朋友 - 3题", "3. wxid_zz - 2题"]
