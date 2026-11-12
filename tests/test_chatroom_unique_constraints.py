"""并发重复建群/建成员:唯一约束 + 统一的"查或建",以及迁移对已有重复数据的合并。

背景:chatrooms/chatroom_members/group_plugin_scopes 原先没有任何唯一约束,微桥每条群消息都"找不到就建",
同一个群的首批消息并发到达时会各建一条(实测 6 个并发消息建出 6 个群)。线上还可能已有重复数据,
所以迁移必须先合并再加约束,而且合并不能丢用户设置。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="chatroom-unique-logs-"))

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation
from app.models.saas import Tenant
from app.services.chatroom_directory import get_or_create_chatroom, get_or_create_member

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------- 并发:不再重复 ----------

@pytest.fixture()
def file_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id=7, code="t", name="t"))
        db.commit()
    with tenant_scope(7), open_tenant_session(factory) as db:
        channel = Channel(bot_id=1, name="c", channel_type="wechat_webridge", is_enabled=True, tenant_id=7)
        db.add(channel)
        db.commit()
        channel_id = int(channel.id)
    return factory, channel_id


def _race(factory, workers: int, job) -> list:
    barrier = threading.Barrier(workers)
    results, errors = [], []

    def run(index: int) -> None:
        try:
            with tenant_scope(7), open_tenant_session(factory) as db:
                barrier.wait()
                results.append(job(db, index))
                db.commit()
        except Exception as exc:  # noqa: BLE001 - 测试里要把线程内异常带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(workers)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert errors == [], errors
    return results


def test_concurrent_first_messages_create_exactly_one_group(file_db) -> None:
    factory, channel_id = file_db

    def job(db, index):
        channel = db.get(Channel, channel_id)
        room, created = get_or_create_chatroom(db, channel, "new@chatroom", name="新群", status="pending_sync")
        return int(room.id), created

    results = _race(factory, 8, job)
    assert len({room_id for room_id, _ in results}) == 1  # 全部拿到同一个群
    assert sum(1 for _, created in results if created) == 1  # 只有一个请求真正建了群,其余是回读(据此决定是否做"首次发现"初始化)
    with tenant_scope(7), open_tenant_session(factory) as db:
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == "new@chatroom")) == 1


def test_concurrent_first_messages_from_one_sender_create_exactly_one_member(file_db) -> None:
    factory, channel_id = file_db
    with tenant_scope(7), open_tenant_session(factory) as db:
        channel = db.get(Channel, channel_id)
        room, _ = get_or_create_chatroom(db, channel, "g@chatroom")
        db.commit()
        room_id = int(room.id)

    def job(db, index):
        channel = db.get(Channel, channel_id)
        room = db.get(Chatroom, room_id)
        member, created = get_or_create_member(db, channel, room, "wxid_same", status="active")
        return int(member.id), created

    results = _race(factory, 8, job)
    assert len({member_id for member_id, _ in results}) == 1 and sum(1 for _, created in results if created) == 1
    with tenant_scope(7), open_tenant_session(factory) as db:
        assert db.scalar(select(func.count(ChatroomMember.id)).where(ChatroomMember.member_external_id == "wxid_same")) == 1


def test_the_real_message_path_is_race_safe_end_to_end(file_db) -> None:
    """走微桥真实的消息入口(observe_message),不只测辅助函数:并发消息既不重复建群,也不重复建发言人。"""

    from app.services.wechat_webridge import message_directory

    factory, channel_id = file_db
    with tenant_scope(7), open_tenant_session(factory) as db:
        db.add(Conversation(tenant_id=7, bot_id=1, channel_id=channel_id, channel_type="wechat_webridge", external_id="hot@chatroom", chat_type="group"))
        db.commit()

    def job(db, index):
        channel = db.get(Channel, channel_id)
        conversation = db.scalar(select(Conversation).where(Conversation.external_id == "hot@chatroom"))
        # 一半是同一个人连发,一半是不同的人:既考验建群,也考验建成员
        sender = "wxid_same" if index % 2 == 0 else f"wxid_{index}"
        event = {"event_type": "message", "message": {"message_type": "text", "sender_id": sender, "sender_name": sender, "content": "hi"},
                 "conversation": {"external_id": "hot@chatroom", "chat_type": "group"}, "details": {}}
        message_directory.observe_message(db, channel, event, conversation)
        return index

    _race(factory, 8, job)
    with tenant_scope(7), open_tenant_session(factory) as db:
        assert db.scalar(select(func.count(Chatroom.id)).where(Chatroom.external_id == "hot@chatroom")) == 1
        assert db.scalar(select(func.count(ChatroomMember.id)).where(ChatroomMember.member_external_id == "wxid_same")) == 1
        assert db.scalar(select(func.count(ChatroomMember.id))) == 1 + 4  # wxid_same + 四个不同的人


def test_the_unique_constraints_exist_on_the_models() -> None:
    from sqlalchemy import UniqueConstraint

    def names(table):
        return {item.name for item in table.constraints if isinstance(item, UniqueConstraint)}

    assert "uq_chatrooms_channel_external" in names(Chatroom.__table__)
    assert "uq_chatroom_members_room_member" in names(ChatroomMember.__table__)
    from app.models.entities import GroupPluginScope

    assert "uq_group_plugin_scopes_room_binding" in names(GroupPluginScope.__table__)


# ---------- 迁移:合并线上已有的重复数据 ----------

def _alembic(database: Path, *args: str) -> None:
    runtime = database.parent / "runtime"
    env = {**os.environ, "APP_ENV": "test", "DATA_DIR": str(runtime / "data"), "DATABASE_URL": f"sqlite:///{database}", "LOG_DIR": str(runtime / "logs"), "AUTH_SEED_DEMO_USERS": "false"}
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=PROJECT_ROOT, env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def _rows(database: Path, sql: str, *params):
    with sqlite3.connect(database) as connection:
        return connection.execute(sql, params).fetchall()


def _seed_duplicates(database: Path) -> None:
    """造一份"线上可能长这样"的数据:同一个群 3 条记录,各自挂了成员和作用域,用户在较晚的那条上改过设置。"""

    with sqlite3.connect(database) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        now = "2026-11-12 10:00:00"
        c.execute("INSERT INTO tenants (id, code, name, status, created_at, updated_at) VALUES (7,'t','t','active',?,?)", (now, now))
        c.execute("INSERT INTO bots (id, tenant_id, name, status, is_enabled, created_at, updated_at) VALUES (1,7,'b','stopped',1,?,?)", (now, now))
        c.execute("INSERT INTO channels (id, tenant_id, bot_id, name, channel_type, dm_policy, login_state, qrcode_status, status, is_enabled, created_at, updated_at) VALUES (1,7,1,'c','wechat_webridge','allowlist','unknown','idle','offline',1,?,?)", (now, now))
        cols = "(id, tenant_id, bot_id, channel_id, external_id, name, status, registration_source, management_status, is_registered, is_active, member_count, extra_json, operator_note, created_at, updated_at)"
        # 1 号:最早建的,资料有名字但是设置是默认;2 号:用户后来把它改成了"完全关闭"并写了备注;3 号:最晚,空壳
        c.execute(f"INSERT INTO chatrooms {cols} VALUES (1,7,1,1,'dup@chatroom','','pending_sync','passive','enabled',1,1,2,'{{\"ai_memory_cursor\":{{\"message_id\":4693}},\"shared\":\"old\"}}',NULL,'2026-11-12 10:00:00','2026-11-12 10:00:00')")
        c.execute(f"INSERT INTO chatrooms {cols} VALUES (2,7,1,1,'dup@chatroom','群名','synced','passive','archived',1,0,5,'{{\"group_features\":{{\"x\":1}},\"shared\":\"new\"}}','用户备注','2026-11-12 10:00:01','2026-11-12 12:00:00')")
        c.execute(f"INSERT INTO chatrooms {cols} VALUES (3,7,1,1,'dup@chatroom',NULL,'pending_sync','passive','enabled',1,1,0,NULL,NULL,'2026-11-12 10:00:02','2026-11-12 10:00:02')")
        c.execute(f"INSERT INTO chatrooms {cols} VALUES (9,7,1,1,'solo@chatroom','单独群','synced','passive','enabled',1,1,3,NULL,NULL,'{now}','{now}')")  # 没有重复的群,不能被动
        mcols = "(id, tenant_id, chatroom_id, bot_id, channel_id, member_external_id, nickname, status, last_active_at, created_at, updated_at)"
        c.execute(f"INSERT INTO chatroom_members {mcols} VALUES (1,7,1,1,1,'wxid_a','甲','left','2026-11-12 09:00:00','{now}','{now}')")
        c.execute(f"INSERT INTO chatroom_members {mcols} VALUES (2,7,2,1,1,'wxid_a','甲','active','2026-11-12 11:00:00','{now}','{now}')")  # 同一个人,这条更好
        c.execute(f"INSERT INTO chatroom_members {mcols} VALUES (3,7,3,1,1,'wxid_b','乙','active',NULL,'{now}','{now}')")  # 只在 3 号上有
        c.execute(f"INSERT INTO chatroom_members {mcols} VALUES (4,7,1,1,1,'wxid_c','丙','active',NULL,'{now}','{now}')")
        c.execute(f"INSERT INTO chatroom_members {mcols} VALUES (5,7,1,1,1,'wxid_c','丙','active',NULL,'{now}','{now}')")  # 同一个群里的同一个人重复(并发首条发言)
        c.execute("INSERT INTO plugins (id, code, display_name, plugin_type, version, default_enabled, created_at, updated_at, platform_status, visibility) VALUES (1,'p','p','message','1',0,?,?,'active','all')", (now, now))
        c.execute("INSERT INTO bot_plugins (id, tenant_id, bot_id, plugin_id, status, runtime_status, config_json, created_at, updated_at, group_default_enabled) VALUES (1,7,1,1,'enabled','running','{}',?,?,1)", (now, now))
        scols = "(id, tenant_id, channel_id, chatroom_id, bot_plugin_id, scope_status, enabled, last_operated_at, created_at, updated_at)"
        c.execute(f"INSERT INTO group_plugin_scopes {scols} VALUES (1,7,1,1,1,'inherited',1,NULL,'{now}','{now}')")
        c.execute(f"INSERT INTO group_plugin_scopes {scols} VALUES (2,7,1,2,1,'custom',0,'2026-11-12 12:30:00','{now}','{now}')")  # 用户最近明确关掉了这个插件
        c.execute(f"INSERT INTO group_trigger_rules (id, tenant_id, chatroom_id, name, trigger_type, trigger_json, actions_json, pick_mode, cooldown_seconds, priority, is_enabled, created_at, updated_at) VALUES (1,7,3,'规则','pat','{{}}','[]','all',0,100,1,'{now}','{now}')")
        c.commit()


def test_the_migration_merges_existing_duplicates_without_losing_user_settings(tmp_path) -> None:
    database = tmp_path / "merge.db"
    _alembic(database, "upgrade", "20261112_0038")
    _seed_duplicates(database)
    _alembic(database, "upgrade", "head")

    # 群:每组只剩 id 最小的一条,没有重复的群原样保留
    assert _rows(database, "SELECT id FROM chatrooms ORDER BY id") == [(1,), (9,)]
    (room,) = _rows(database, "SELECT name, status, management_status, is_active, operator_note, extra_json, member_count FROM chatrooms WHERE id=1")
    name, status, management, active, note, extra, count = room
    assert (management, active, note) == ("archived", 0, "用户备注")      # 用户设置以最近更新的那条(2 号)为准
    import json

    merged = json.loads(extra)
    assert merged["group_features"] == {"x": 1}                           # 用户在 2 号上设置的群功能没有丢
    assert merged["ai_memory_cursor"] == {"message_id": 4693}             # 1 号上的 AI 记忆游标也没有丢(否则群聊会被重复写入长期记忆)
    assert merged["shared"] == "new"                                      # 同名的键取最近更新的那行
    assert name == "群名"                                                 # 保留群缺失的资料从其他重复行补齐
    assert count == 5                                                     # 成员数取最大
    assert _rows(database, "SELECT member_count, management_status FROM chatrooms WHERE id=9") == [(3, "enabled")]

    # 成员:全部指向保留群;同一人多条时保留更好的那条,没有重复
    members = _rows(database, "SELECT chatroom_id, member_external_id, status FROM chatroom_members ORDER BY member_external_id")
    assert members == [(1, "wxid_a", "active"), (1, "wxid_b", "active"), (1, "wxid_c", "active")]

    # 群插件作用域:保留用户最近明确操作的那条(关闭)
    assert _rows(database, "SELECT chatroom_id, enabled, scope_status FROM group_plugin_scopes") == [(1, 0, "custom")]

    # 触发规则等引用群的行跟着改指向,不会因为群被删而悬空
    assert _rows(database, "SELECT chatroom_id FROM group_trigger_rules") == [(1,)]


def test_the_migration_adds_constraints_that_really_block_duplicates(tmp_path) -> None:
    database = tmp_path / "block.db"
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        now = "2026-11-12 10:00:00"
        c.execute("INSERT INTO tenants (id, code, name, status, created_at, updated_at) VALUES (7,'t','t','active',?,?)", (now, now))
        c.execute("INSERT INTO bots (id, tenant_id, name, status, is_enabled, created_at, updated_at) VALUES (1,7,'b','stopped',1,?,?)", (now, now))
        c.execute("INSERT INTO channels (id, tenant_id, bot_id, name, channel_type, dm_policy, login_state, qrcode_status, status, is_enabled, created_at, updated_at) VALUES (1,7,1,'c','x','allowlist','unknown','idle','offline',1,?,?)", (now, now))
        cols = "(tenant_id, bot_id, channel_id, external_id, status, registration_source, management_status, is_registered, is_active, member_count, created_at, updated_at)"
        c.execute(f"INSERT INTO chatrooms {cols} VALUES (7,1,1,'g@chatroom','s','passive','enabled',1,1,0,'{now}','{now}')")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"INSERT INTO chatrooms {cols} VALUES (7,1,1,'g@chatroom','s','passive','enabled',1,1,0,'{now}','{now}')")
        c.execute(f"INSERT INTO chatrooms {cols} VALUES (7,1,1,'other@chatroom','s','passive','enabled',1,1,0,'{now}','{now}')")  # 不同的群不受影响


def test_the_migration_is_a_noop_without_duplicates_and_can_downgrade(tmp_path) -> None:
    database = tmp_path / "clean.db"
    _alembic(database, "upgrade", "head")
    _alembic(database, "downgrade", "20261112_0038")
    _alembic(database, "upgrade", "head")
    with sqlite3.connect(database) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261112_0039"


# ---------- 防回归:加锁重读不能冲掉本事务里还没写入的修改 ----------

def test_re_reading_a_group_never_discards_unflushed_changes_in_the_same_transaction(file_db) -> None:
    """项目的会话关闭了 autoflush,而重读用了 populate_existing(拿数据库值覆盖对象)。

    如果重读前不先 flush,刚在本事务里改的东西(例如群功能开关、运行模式)会被数据库里的旧值静默冲掉。
    这个性质一度被破坏过,只是碰巧被别的功能测试发现。这里直接固定它。
    """

    factory, channel_id = file_db
    with tenant_scope(7), open_tenant_session(factory) as db:
        channel = db.get(Channel, channel_id)
        room, _ = get_or_create_chatroom(db, channel, "keep@chatroom", status="synced", management_status="enabled")
        db.commit()
        room.management_status = "archived"       # 本事务里的修改,尚未 flush
        room.operator_note = "用户刚写的备注"
        again, created = get_or_create_chatroom(db, channel, "keep@chatroom", status="should-not-apply")
        assert created is False and again is room
        assert (room.management_status, room.operator_note) == ("archived", "用户刚写的备注")  # 修改还在
        member, _ = get_or_create_member(db, channel, room, "wxid_x", status="active")
        member.nickname = "刚改的昵称"
        member_again, member_created = get_or_create_member(db, channel, room, "wxid_x", status="left")
        assert member_created is False and member_again.nickname == "刚改的昵称" and member_again.status == "active"
        db.commit()
    with tenant_scope(7), open_tenant_session(factory) as db:
        stored = db.scalar(select(Chatroom).where(Chatroom.external_id == "keep@chatroom"))
        assert (stored.management_status, stored.operator_note) == ("archived", "用户刚写的备注")
