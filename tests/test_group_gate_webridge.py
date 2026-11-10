"""群“启用”总开关与群内插件开关对微桥渠道的调度回归测试。

此前 _load_group_chatroom 只认 iPad/winauto/企微,微桥群即使在后台停用,插件仍会被触发。
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="group-gate-logs-"))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import platform_session
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, GroupPluginScope, Plugin
from app.models.saas import Tenant
from app.services.plugin_directory import build_static_dispatch_plan

ROOM = "49740991276@chatroom"
EVENT = {
    "conversation": {"external_id": ROOM, "chat_type": "group"},
    "message": {"content": "/签到", "sender_id": "wxid_sender", "message_type": "text"},
    "details": {"mentioned_self": False},
}


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with platform_session(factory) as db:
        tenant = Tenant(code="gate", name="闸门租户", status="active")
        plugin = Plugin(code="message_sign", display_name="消息签到", plugin_type="command")
        db.add_all([tenant, plugin])
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(tenant_id=tenant.id, bot_id=bot.id, name="微桥", channel_type="wechat_webridge", status="online", is_enabled=True)
        db.add(channel)
        db.flush()
        room = Chatroom(tenant_id=tenant.id, bot_id=bot.id, channel_id=channel.id, external_id=ROOM, name="BOT-测试", is_active=True, management_status="enabled")
        binding = BotPlugin(tenant_id=tenant.id, bot_id=bot.id, plugin_id=plugin.id, status="enabled")
        db.add_all([room, binding])
        db.commit()
        ids = {"tenant": int(tenant.id), "channel": int(channel.id), "room": int(room.id), "binding": int(binding.id)}
    try:
        yield factory, ids
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _plan(factory, ids):
    with tenant_scope(ids["tenant"]):
        with platform_session(factory) as db:
            channel = db.get(Channel, ids["channel"])
            return build_static_dispatch_plan(db, channel, EVENT)


def test_enabled_group_runs_plugin(env) -> None:
    factory, ids = env
    assert "message_sign" in _plan(factory, ids).candidate_plugin_codes


def test_disabled_webridge_group_skips_plugins(env) -> None:
    factory, ids = env
    with tenant_scope(ids["tenant"]):
        with platform_session(factory) as db:
            db.get(Chatroom, ids["room"]).is_active = False
            db.commit()
    plan = _plan(factory, ids)
    assert plan.candidate_plugin_codes == ()
    assert dict(plan.skipped)["message_sign"] == "group_chatroom_inactive"


def test_group_scope_disabled_plugin_is_skipped_on_webridge(env) -> None:
    factory, ids = env
    with tenant_scope(ids["tenant"]):
        with platform_session(factory) as db:
            db.add(
                GroupPluginScope(
                    tenant_id=ids["tenant"], channel_id=ids["channel"], chatroom_id=ids["room"],
                    bot_plugin_id=ids["binding"], scope_status="disabled", enabled=False,
                )
            )
            db.commit()
    plan = _plan(factory, ids)
    assert "message_sign" not in plan.candidate_plugin_codes
    assert dict(plan.skipped)["message_sign"] == "group_scope_disabled"
