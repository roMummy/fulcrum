"""群管理 / 好友目录查询的轻量测试:搜索、渠道筛选、分页和通配符转义。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="directory-test-logs-"))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.entities import Channel, Chatroom
from app.services.directory_pages import PAGE_SIZE, build_chatroom_directory


@pytest.fixture()
def db():
    """只建群目录用到的表,避免被其他历史夹具问题牵连。"""

    engine = create_engine("sqlite://")
    Channel.__table__.create(engine)
    Chatroom.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _room(db, idx: int, name: str, channel_id: int = 1, status: str = "discovered") -> Chatroom:
    room = Chatroom(bot_id=1, channel_id=channel_id, external_id=f"{idx}@chatroom", name=name, management_status=status, tenant_id=1, is_active=status == "enabled")
    db.add(room)
    return room


def test_search_and_status_filter(db) -> None:
    db.add(Channel(id=1, name="微桥一号", channel_type="wechat_webridge", bot_id=1, tenant_id=1))
    _room(db, 1, "运营群", status="enabled")
    _room(db, 2, "测试群")
    db.commit()

    result = build_chatroom_directory(db, keyword="运营")
    assert [row["identity"]["name"] for row in result["rows"]] == ["运营群"]
    assert build_chatroom_directory(db, status="discovered")["rows"][0]["identity"]["name"] == "测试群"
    # 非法状态值被忽略,不应把列表筛空。
    assert build_chatroom_directory(db, status="bogus")["paging"]["total"] == 2


def test_like_wildcards_are_escaped(db) -> None:
    db.add(Channel(id=1, name="微桥一号", channel_type="wechat_webridge", bot_id=1, tenant_id=1))
    _room(db, 1, "群A")
    db.commit()
    assert build_chatroom_directory(db, keyword="%")["paging"]["total"] == 0


def test_paging_clamps_out_of_range_page(db) -> None:
    db.add(Channel(id=1, name="微桥一号", channel_type="wechat_webridge", bot_id=1, tenant_id=1))
    for i in range(PAGE_SIZE + 5):
        _room(db, i, f"群{i:03d}")
    db.commit()
    result = build_chatroom_directory(db, page=99)
    assert result["paging"]["page"] == 2
    assert len(result["rows"]) == 5
