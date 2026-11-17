"""历史补录不能改变最新消息、双向游标或 AI 的近期上下文。"""
from datetime import timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Conversation, Message
from app.models.saas import Tenant
from app.services import ai_chat
from app.services.ai_chat import build_chat_input, collect_speaker_names
from app.services.message_history import latest_history_messages, list_history_messages
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed


def seed_history():
    db = _session()
    _seed(db)
    conversation = Conversation(id=2, tenant_id=1, bot_id=1, channel_id=1, channel_type="wechat_webridge", external_id="fixture@chatroom", chat_type="group")
    db.add(conversation)
    db.flush()
    now = now_shanghai()
    latest = Message(tenant_id=1, conversation_id=2, direction="in", sender_id="fixture", content="今天的实时消息", created_at=now)
    db.add(latest)
    db.flush()
    older = Message(tenant_id=1, conversation_id=2, direction="in", sender_id="fixture", content="补入的历史消息", created_at=now - timedelta(days=5), status="archived")
    tied = Message(tenant_id=1, conversation_id=2, direction="in", sender_id="fixture", content="同一时间的另一条消息", created_at=now)
    db.add_all([older, tied])
    db.commit()
    return db, latest, older, tied


def test_latest_page_uses_time_before_insert_id():
    db, latest, older, tied = seed_history()
    try:
        rows, more = list_history_messages(db, 2, limit=2)
        assert [r.id for r in rows] == [latest.id, tied.id]
        assert more
    finally:
        db.close()


def test_bidirectional_cursor_includes_late_import_and_breaks_ties():
    db, latest, older, tied = seed_history()
    try:
        rows, more = list_history_messages(db, 2, before_id=latest.id)
        assert [r.id for r in rows] == [older.id] and not more
        rows, more = list_history_messages(db, 2, after_id=older.id, limit=1)
        assert [r.id for r in rows] == [latest.id] and more
        rows, more = list_history_messages(db, 2, after_id=latest.id)
        assert [r.id for r in rows] == [tied.id] and not more
    finally:
        db.close()


def test_cursor_from_other_conversation_is_rejected():
    db, latest, _, _ = seed_history()
    try:
        assert list_history_messages(db, 999, after_id=latest.id) == ([], False)
    finally:
        db.close()


def test_ai_recent_history_keeps_chronological_order():
    db, _, _, _ = seed_history()
    try:
        chat = build_chat_input(db, channel_id=1, conversation_id=2, sender_id="fixture", text="新的请求", persona="fixture")
        assert "补入的历史消息" in chat.history[0]
        assert "同一时间的另一条消息" in chat.history[-1]
    finally:
        db.close()


def test_latest_summary_and_refresh_keep_time_order_and_hide_echoes():
    db, latest, older, tied = seed_history()
    try:
        echo = Message(tenant_id=1, conversation_id=2, direction="in", sender_id="fixture", content="duplicate echo", created_at=tied.created_at + timedelta(seconds=1), status="echo")
        db.add(echo)
        db.commit()
        assert latest_history_messages(db, [2, 999])[2].id == tied.id
        assert latest_history_messages(db, []) == {}
        rows, more = list_history_messages(db, 2, message_ids=[echo.id, older.id, tied.id, latest.id])
        assert [row.id for row in rows] == [older.id, latest.id, tied.id] and not more
        rows, _ = list_history_messages(db, 2)
        assert [row.id for row in rows] == [older.id, latest.id, tied.id]
    finally:
        db.close()


@pytest.mark.parametrize("limit", [1, 2])
def test_cursor_walk_has_no_gaps_or_duplicates_across_time_boundaries(limit):
    db, latest, older, tied = seed_history()
    try:
        extra = [
            Message(tenant_id=1, conversation_id=2, direction="in", content="同一时间补录", created_at=latest.created_at),
            Message(tenant_id=1, conversation_id=2, direction="in", content="中间时间补录", created_at=older.created_at + timedelta(days=1)),
        ]
        db.add_all(extra)
        db.commit()
        expected = [row.id for row in sorted([latest, older, tied, *extra], key=lambda row: (row.created_at, row.id))]
        rows, more = list_history_messages(db, 2, limit=limit)
        backward = [row.id for row in rows]
        while more:
            rows, more = list_history_messages(db, 2, before_id=backward[0], limit=limit)
            assert rows
            backward = [row.id for row in rows] + backward
        assert backward == expected

        forward = [older.id]
        while True:
            rows, more = list_history_messages(db, 2, after_id=forward[-1], limit=limit)
            forward.extend(row.id for row in rows)
            if not more:
                break
            assert rows
        assert forward == expected
    finally:
        db.close()


def test_ai_history_window_ignores_later_inserted_old_messages(monkeypatch):
    db, _, _, _ = seed_history()
    try:
        monkeypatch.setattr(ai_chat, "HISTORY_LIMIT", 2)
        chat = build_chat_input(db, channel_id=1, conversation_id=2, sender_id="fixture", text="新的请求", persona="fixture")
        assert len(chat.history) == 2
        assert "今天的实时消息" in chat.history[0]
        assert "同一时间的另一条消息" in chat.history[1]
    finally:
        db.close()


def test_speaker_name_uses_latest_message_time():
    db, latest, older, _ = seed_history()
    try:
        latest.sender_name = "当前昵称"
        older.sender_name = "历史昵称"
        db.commit()
        assert collect_speaker_names(db, conversation_id=2) == (("fixture", "当前昵称"),)
    finally:
        db.close()


def test_history_helpers_keep_tenant_scope_for_summaries_cursors_and_refresh():
    db, latest, _, tied = seed_history()
    try:
        db.add(Tenant(id=2, code="other-fixture", name="其他测试租户"))
        db.add(Bot(id=2, tenant_id=2, name="其他测试机器人"))
        db.flush()
        db.add(Channel(id=2, tenant_id=2, bot_id=2, name="其他测试账号", channel_type="wechat_webridge"))
        db.flush()
        db.add(Conversation(id=3, tenant_id=2, bot_id=2, channel_id=2, channel_type="wechat_webridge", external_id="other-fixture", chat_type="direct"))
        db.flush()
        foreign = Message(tenant_id=2, conversation_id=3, direction="in", content="其他租户消息")
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
        with tenant_scope(1), tenant_session(sessionmaker(bind=db.get_bind())) as scoped:
            summaries = latest_history_messages(scoped, [2, 3])
            assert {key: row.id for key, row in summaries.items()} == {2: tied.id}
            assert list_history_messages(scoped, 3) == ([], False)
            assert list_history_messages(scoped, 2, after_id=foreign_id) == ([], False)
            rows, more = list_history_messages(scoped, 2, message_ids=[latest.id, foreign_id])
            assert [row.id for row in rows] == [latest.id] and not more
    finally:
        db.close()
