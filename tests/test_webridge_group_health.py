"""微桥群健康度与成员最后发言口径的轻量测试。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="group-health-logs-"))

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Chatroom, ChatroomMember, Conversation, Message, PluginExecutionLog
from app.services.wechat_webridge.chatrooms import _build_health, _relative_days_label


def test_health_counts_active_and_silent_members():
    engine = create_engine("sqlite://")
    for table in (Chatroom, ChatroomMember, Conversation, Message, PluginExecutionLog):
        table.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    now = now_shanghai()
    with tenant_scope(1):
        with tenant_session(factory) as db:
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=1, external_id="g@chatroom", name="群")
            db.add(room)
            db.flush()
            # 3 天前、40 天前、没有记录、已退群各一人。
            for wxid, last, status in (
                ("a", now - timedelta(days=3), "active"),
                ("b", now - timedelta(days=40), "active"),
                ("c", None, "active"),
                ("d", now - timedelta(days=1), "left"),
            ):
                db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=1, member_external_id=wxid, last_active_at=last, status=status))
            db.commit()
            members = db.query(ChatroomMember).all()
            health = _build_health(db, SimpleNamespace(id=1), room, members)
    # 已退群不计入;沉默 = 30 天无发言(含无记录)。
    assert health["total"] == 3
    assert health["active_7d"] == 1
    assert health["active_30d"] == 1
    assert health["silent"] == 2
    assert 0 <= health["score"] <= 100


def test_relative_days_label():
    now = now_shanghai()
    assert _relative_days_label(None, now) == "暂无记录"
    assert _relative_days_label(now, now) == "今天"
    assert _relative_days_label(now - timedelta(days=5, hours=1), now) == "5 天前"
