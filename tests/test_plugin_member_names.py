"""插件取群成员昵称:wxid 不能当昵称展示,群昵称优先,批量一次查询。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="member-names-logs-"))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, Chatroom, ChatroomMember
from app.services.plugin_runtime import member_names


@pytest.fixture()
def env():
    engine = create_engine("sqlite://")
    for table in (Channel, Chatroom, ChatroomMember):
        table.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge")
            db.add(channel)
            db.flush()
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="群", is_active=True)
            other = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="h@chatroom", name="别的群", is_active=True)
            db.add_all([room, other])
            db.flush()
            def add(chat, uid, display=None, nick=None):
                db.add(ChatroomMember(tenant_id=1, chatroom_id=chat.id, bot_id=1, channel_id=channel.id, member_external_id=uid, display_name=display, nickname=nick, status="active"))
            add(room, "wxid_a1", display="小明")
            add(room, "wxid_b2", nick="小红")                 # 只有昵称
            add(room, "wxid_c3", display="wxid_c3", nick="阿三")  # 群昵称本身是 wxid:跳过,用昵称
            add(room, "wxid_d4", display="wxid_d4")           # 只有 wxid
            add(other, "wxid_e5", display="别群的人")
            db.flush()
            yield db, int(channel.id)


def test_names_come_from_the_group_member_table_and_never_show_a_raw_id_when_a_name_exists(env) -> None:
    db, channel_id = env
    result = member_names(db, channel_id=channel_id, group_external_id="g@chatroom", user_ids=["wxid_a1", "wxid_b2", "wxid_c3"])
    assert result == {"wxid_a1": "小明", "wxid_b2": "小红", "wxid_c3": "阿三"}


def test_fallbacks_are_used_only_when_they_are_real_names_and_every_id_gets_an_answer(env) -> None:
    db, channel_id = env
    result = member_names(
        db, channel_id=channel_id, group_external_id="g@chatroom",
        user_ids=["wxid_d4", "wxid_zz", "wxid_yy", "wxid_a1"],
        fallbacks={"wxid_zz": "当时记下的名字", "wxid_yy": "wxid_yy", "wxid_a1": "旧名字"},
    )
    assert result["wxid_zz"] == "当时记下的名字"      # 成员表没有:用回退名
    assert result["wxid_yy"] == "wxid_yy" and result["wxid_d4"] == "wxid_d4"  # 回退名也是 wxid:只能显示 ID
    assert result["wxid_a1"] == "小明"                # 成员表优先于回退名(昵称更新后排行榜跟着变)
    assert member_names(db, channel_id=channel_id, group_external_id="g@chatroom", user_ids=[]) == {}


def test_other_groups_are_not_mixed_in(env) -> None:
    db, channel_id = env
    assert member_names(db, channel_id=channel_id, group_external_id="g@chatroom", user_ids=["wxid_e5"]) == {"wxid_e5": "wxid_e5"}
