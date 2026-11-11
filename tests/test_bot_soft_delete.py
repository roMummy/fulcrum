"""机器人逻辑删除:删除账号时同步隐藏它的机器人,各处列表、统计、选择器不再出现已删除的机器人。

线上现象:账号被删除后,机器人「测试」仍留在机器人列表、插件页、消息中心、统计等很多地方。
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="bot-soft-delete-logs-"))

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from app.core.time import now_shanghai
from app.models.entities import Bot, BotPlugin, Channel, Conversation, Plugin
from app.services import channel_deletion, crud
from tests.test_ai_chat import _session
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)


# ---------- 删除账号时的规则 ----------

def _bot_with_channels(db, name: str, *, live: int = 1, enabled: bool = False) -> tuple[Bot, list[Channel]]:
    bot = Bot(tenant_id=1, name=name, status="running" if enabled else "stopped", is_enabled=enabled)
    db.add(bot)
    db.flush()
    channels = []
    for number in range(live):
        channel = Channel(tenant_id=1, bot_id=bot.id, name=f"{name}-账号{number}", channel_type="wechat_webridge", is_enabled=False, status="offline")
        db.add(channel)
        channels.append(channel)
    db.flush()
    return bot, channels


def test_deleting_the_only_account_deletes_its_bot_too() -> None:
    db = _session()
    bot, (channel,) = _bot_with_channels(db, "测试", enabled=True)
    channel_deletion.delete_channel_account(db, channel)
    db.flush()
    assert channel.deleted_at is not None
    assert bot.deleted_at is not None and bot.is_enabled is False and bot.status == "deleted"
    assert crud.get_bot(db, bot.id) is None  # 详情/操作入口按不存在处理
    assert bot.id not in [item.id for item in crud.list_bots(db)]


def test_the_bot_stays_while_it_still_has_another_live_account() -> None:
    db = _session()
    bot, (first, second) = _bot_with_channels(db, "双账号", live=2)
    channel_deletion.delete_channel_account(db, first)
    assert bot.deleted_at is None and crud.get_bot(db, bot.id) is bot
    channel_deletion.delete_channel_account(db, second)  # 最后一个账号也删了,机器人跟着删
    assert bot.deleted_at is not None


def test_a_bot_without_any_account_is_not_touched_and_deleted_channels_leave_the_list() -> None:
    db = _session()
    unconnected = Bot(tenant_id=1, name="尚未接入", status="stopped", is_enabled=False)
    db.add(unconnected)
    bot, (channel,) = _bot_with_channels(db, "将被删除")
    db.flush()
    channel_deletion.delete_channel_account(db, channel)
    assert unconnected.deleted_at is None and unconnected.id in [item.id for item in crud.list_bots(db)]
    assert channel.id not in [item.id for item in crud.list_channels(db)]


# ---------- 迁移:修正线上已有的"账号已删、机器人还在" ----------

def test_migration_marks_bots_whose_accounts_are_all_deleted() -> None:
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "20261111_0034_bots_soft_delete.py"
    spec = importlib.util.spec_from_file_location("bots_soft_delete_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE bots (id INTEGER PRIMARY KEY, name TEXT, status TEXT, is_enabled INTEGER)"))
        conn.execute(text("CREATE TABLE channels (id INTEGER PRIMARY KEY, bot_id INTEGER, deleted_at TIMESTAMP)"))
        conn.execute(text("INSERT INTO bots VALUES (1,'全部已删','stopped',0),(2,'有在用账号','running',1),(3,'没有账号','stopped',0),(4,'一删一在','running',1)"))
        conn.execute(text("INSERT INTO channels VALUES (1,1,'2026-11-10 19:37:05'),(2,2,NULL),(3,4,'2026-11-10 10:00:00'),(4,4,NULL)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = {row[0]: row[1:] for row in conn.execute(text("SELECT id, deleted_at, is_enabled, status FROM bots")).all()}
    assert rows[1] == ("2026-11-10 19:37:05", 0, "deleted")  # 删除时间取账号的删除时间
    assert rows[2][0] is None and rows[3][0] is None and rows[4][0] is None  # 有在用账号的、没有账号的都不动
    assert rows[2][2] == "running" and rows[4][2] == "running"


# ---------- 页面 ----------

@pytest.fixture()
def pages(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    with ids["factory"]() as db:
        tenant_id = ids["alpha"]
        plugin = Plugin(code="demo-plugin", display_name="示例插件")
        db.add(plugin)
        db.flush()
        for name, deleted in (("在用机器人XYZ", False), ("已删机器人XYZ", True)):
            bot = Bot(tenant_id=tenant_id, name=name, status="stopped" if deleted else "running", is_enabled=not deleted, deleted_at=now_shanghai() if deleted else None)
            db.add(bot)
            db.flush()
            channel = Channel(tenant_id=tenant_id, bot_id=bot.id, name=f"{name}账号", channel_type="wechat_webridge", is_enabled=not deleted, status="offline" if deleted else "online", deleted_at=now_shanghai() if deleted else None)
            db.add(channel)
            db.flush()
            db.add(BotPlugin(tenant_id=tenant_id, bot_id=bot.id, plugin_id=plugin.id, status="enabled", runtime_status="running", config_json="{}"))
            db.add(Conversation(tenant_id=tenant_id, bot_id=bot.id, channel_id=channel.id, channel_type="wechat_webridge", external_id=f"{name}@chatroom", chat_type="group", title=f"{name}的群"))
        db.commit()
    return client


def test_deleted_bots_disappear_from_lists_plugins_and_messages(pages) -> None:
    client = pages
    _login(client, "admin")
    bots = client.get("/bots").text
    assert "在用机器人XYZ" in bots and "已删机器人XYZ" not in bots
    plugins = client.get("/plugins").text
    assert "在用机器人XYZ" in plugins and "已删机器人XYZ" not in plugins
    runtime = client.get("/plugins/runtime").text
    assert "已删机器人XYZ" not in runtime
    messages = client.get("/messages").text
    assert "在用机器人XYZ的群" in messages and "已删机器人XYZ的群" not in messages
    assert "已删机器人XYZ" not in client.get("/").text  # 总览(排行、机器人概况)


def test_a_deleted_bot_detail_and_actions_return_404(pages) -> None:
    client = pages
    csrf = _login(client, "admin")
    with importlib.import_module("app.core.db").SessionLocal() as db:
        deleted_id = db.query(Bot).filter(Bot.name == "已删机器人XYZ").one().id
    assert client.get(f"/bots/{deleted_id}").status_code == 404
    assert client.post(f"/bots/{deleted_id}/toggle", data={"csrf_token": csrf}).status_code in {403, 404}
