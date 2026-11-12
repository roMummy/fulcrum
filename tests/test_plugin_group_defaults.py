"""首次绑定的群开放选择覆盖缺省、新群、恢复继承和定时推送。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, TaskJob
from app.schemas.forms import BotPluginBindCreate
from app.services.outbound_batch import enqueue_outbound_jobs_by_conversation
from app.services.plugin_directory import build_static_dispatch_plan
from app.services.wechat_ipad.chatrooms import (
    materialize_group_plugin_scopes_for_chatroom,
    reset_group_plugin_scope_to_inherited,
    update_group_plugin_scope_config_override,
    update_group_plugin_scope_enabled,
)
from test_group_gate_webridge import EVENT, env  # noqa: F401


def test_new_binding_form_defaults_to_closed_groups():
    assert not BotPluginBindCreate(bot_id=1, plugin_id=1).group_default_enabled


def test_migration_preserves_existing_bindings(tmp_path):
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261112_0037_plugin_group_defaults.py"
    spec = importlib.util.spec_from_file_location("plugin_group_defaults_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE bot_plugins (id INTEGER PRIMARY KEY, status VARCHAR(20))"))
            connection.execute(text("INSERT INTO bot_plugins (id, status) VALUES (1, 'enabled'), (2, 'disabled')"))
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            assert connection.execute(text("SELECT group_default_enabled FROM bot_plugins ORDER BY id")).scalars().all() == [1, 1]
    finally:
        engine.dispose()


def test_default_closed_blocks_existing_and_future_groups_and_reset(env):
    factory, ids = env
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        binding = db.get(BotPlugin, ids["binding"])
        binding.group_default_enabled = False
        channel = db.get(Channel, ids["channel"])
        room = db.get(Chatroom, ids["room"])
        db.flush()
        plan = build_static_dispatch_plan(db, channel, EVENT)
        assert dict(plan.skipped)["message_sign"] == "group_scope_disabled"
        scopes = materialize_group_plugin_scopes_for_chatroom(db, channel=channel, chatroom=room)
        assert len(scopes) == 1 and not scopes[0].enabled
        scope = update_group_plugin_scope_enabled(db, channel=channel, chatroom=room, bot_plugin_id=binding.id, enabled=True)
        assert "message_sign" in build_static_dispatch_plan(db, channel, EVENT).candidate_plugin_codes
        update_group_plugin_scope_config_override(db, channel=channel, chatroom=room, bot_plugin_id=binding.id, config_override_json='{"x":1}')
        assert scope.enabled and scope.scope_status == "overridden"
        reset_group_plugin_scope_to_inherited(db, channel=channel, chatroom=room, bot_plugin_id=binding.id)
        assert not scope.enabled and scope.config_override_json is None
        assert "message_sign" not in build_static_dispatch_plan(db, channel, EVENT).candidate_plugin_codes
        future_room = Chatroom(tenant_id=ids["tenant"], bot_id=channel.bot_id, channel_id=channel.id, external_id="future@chatroom", is_active=True)
        db.add(future_room)
        db.flush()
        new_scopes = materialize_group_plugin_scopes_for_chatroom(db, channel=channel, chatroom=future_room)
        assert len(new_scopes) == 1 and not new_scopes[0].enabled


def test_opt_in_and_legacy_binding_still_open_groups(env):
    factory, ids = env
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        channel = db.get(Channel, ids["channel"])
        binding = db.get(BotPlugin, ids["binding"])
        assert binding.group_default_enabled
        assert "message_sign" in build_static_dispatch_plan(db, channel, EVENT).candidate_plugin_codes
        binding.group_default_enabled = True
        scopes = materialize_group_plugin_scopes_for_chatroom(db, channel=channel, chatroom=db.get(Chatroom, ids["room"]))
        assert scopes[0].enabled


def test_unbound_binding_does_not_materialize_group_scopes(env):
    factory, ids = env
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        db.get(BotPlugin, ids["binding"]).status = "unbound"
        db.flush()
        assert materialize_group_plugin_scopes_for_chatroom(db, channel=db.get(Channel, ids["channel"]), chatroom=db.get(Chatroom, ids["room"])) == []


def test_batch_push_skips_closed_group_without_rolling_back_other_targets(env):
    factory, ids = env
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        channel = db.get(Channel, ids["channel"])
        binding = db.get(BotPlugin, ids["binding"])
        binding.group_default_enabled = False
        room = db.get(Chatroom, ids["room"])
        allowed = Conversation(tenant_id=ids["tenant"], bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type, external_id=room.external_id, chat_type="group")
        denied = Conversation(tenant_id=ids["tenant"], bot_id=channel.bot_id, channel_id=channel.id, channel_type=channel.channel_type, external_id="future@chatroom", chat_type="group")
        db.add_all([allowed, denied])
        db.flush()
        update_group_plugin_scope_enabled(db, channel=channel, chatroom=room, bot_plugin_id=binding.id, enabled=True)
        result = enqueue_outbound_jobs_by_conversation(db, conversations=[allowed, denied], plugin_code="message_sign", related_type="test", related_id=binding.id, priority=70, payload_builder=lambda conversation, channel: [{"message_type": "text", "content": "test"}])
        assert result["pushed_conversation_count"] == 1
        assert result["skipped_conversation_count"] == 1
        assert result["failed_targets"] == []
        assert len(result["job_ids"]) == 1
        assert db.get(TaskJob, result["job_ids"][0]) is not None
        assert db.scalar(select(GroupPluginScope).where(GroupPluginScope.bot_plugin_id == binding.id)).enabled
