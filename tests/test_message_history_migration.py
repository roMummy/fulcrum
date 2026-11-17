"""消息时间索引兼容全新升级与已经手动建立索引的安装。"""
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.models import saas
from app.models.entities import Bot, Channel, Conversation, Message
from app.services.database_lifecycle import expected_database_revision
from tests.test_alembic_lifecycle import _current_revision, _run_alembic


INDEX_NAME = "ix_messages_conversation_time"
INDEX_COLUMNS = ["conversation_id", "created_at", "id"]


@pytest.mark.parametrize("preexisting_index", [False, True])
def test_history_index_upgrade_preserves_messages_and_supports_downgrade(tmp_path: Path, preexisting_index: bool) -> None:
    database_path = tmp_path / "history.db"
    _run_alembic(database_path, "upgrade", "20261113_0040")
    engine = create_engine(f"sqlite:///{database_path}")
    with Session(engine) as db:
        tenant = saas.Tenant(code="history-fixture", name="测试租户", status="active")
        db.add(tenant)
        db.flush()
        bot = Bot(tenant_id=tenant.id, name="测试机器人")
        db.add(bot)
        db.flush()
        channel = Channel(tenant_id=tenant.id, bot_id=bot.id, name="测试账号", channel_type="wechat_webridge")
        db.add(channel)
        db.flush()
        conversation = Conversation(tenant_id=tenant.id, bot_id=bot.id, channel_id=channel.id, channel_type="wechat_webridge", external_id="fixture", chat_type="direct")
        db.add(conversation)
        db.flush()
        db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, direction="in", content="已有历史消息", status="archived"))
        db.commit()
    engine.dispose()

    def saved_messages():
        with sqlite3.connect(database_path) as connection:
            return connection.execute("SELECT * FROM messages ORDER BY id").fetchall()

    expected = saved_messages()
    assert len(expected) == 1
    if preexisting_index:
        with sqlite3.connect(database_path) as connection:
            connection.execute(f"CREATE INDEX {INDEX_NAME} ON messages (conversation_id, created_at, id)")

    _run_alembic(database_path, "upgrade", "head")
    assert _current_revision(database_path) == expected_database_revision()
    assert saved_messages() == expected
    engine = create_engine(f"sqlite:///{database_path}")
    index = next(item for item in inspect(engine).get_indexes("messages") if item["name"] == INDEX_NAME)
    assert index["column_names"] == INDEX_COLUMNS and not index["unique"]
    model_index = next(item for item in Message.__table__.indexes if item.name == INDEX_NAME)
    assert [column.name for column in model_index.columns] == INDEX_COLUMNS
    engine.dispose()

    _run_alembic(database_path, "downgrade", "20261113_0040")
    assert saved_messages() == expected
    engine = create_engine(f"sqlite:///{database_path}")
    assert INDEX_NAME not in {item["name"] for item in inspect(engine).get_indexes("messages")}
    engine.dispose()
    _run_alembic(database_path, "upgrade", "head")
    assert _current_revision(database_path) == expected_database_revision()
    assert saved_messages() == expected
