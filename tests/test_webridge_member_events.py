"""微桥群成员事件(加入/离开/改群昵称)的适配与群功能处理轻量测试。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="member-events-logs-"))

from types import SimpleNamespace

from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.group_features.member_change import apply_member_change, build_reply_text, parse_member_change


def _normalize(event_name: str, **body):
    adapter = WechatWeBridgeAdapter()
    base = {"room_id": "g@chatroom", "is_group": True, "member_id": "wxid_a", "member_display_name": "小明", "snapshot_after_count": 24}
    return adapter.normalize_inbound_payload(SimpleNamespace(id=2), {"event_name": event_name, "body": {**base, **body}})


def test_leave_event_becomes_structured_change():
    event = _normalize("group_member_leave")
    assert event["message"]["message_type"] == "group_member_leave"
    assert event["message"]["content"] == "小明 退出了群聊"
    change = event["details"]["member_change"]
    assert change["change_type"] == "left" and change["after_count"] == 24
    parsed = parse_member_change(event)
    assert parsed["members"] == [{"external_id": "wxid_a", "display_name": "小明"}] and parsed["after_count"] == 24


def test_join_event_and_welcome_reply():
    event = _normalize("group_member_join")
    parsed = parse_member_change(event)
    assert parsed["change_type"] == "join"
    assert build_reply_text("join", parsed["members"]) == "📋 群成员变动通知:\n🆕 成员新增: 小明"


def test_nickname_change_event():
    event = _normalize("group_member_nickname_change", old_room_nick_name="a", new_room_nick_name="新群昵称")
    assert event["message"]["message_type"] == "group_member_nickname_change"
    parsed = parse_member_change(event)
    assert parsed["change_type"] == "rename" and parsed["new_name"] == "新群昵称"
    # 改群昵称发变动通知:旧名取库里记录,没有则写“未设置”
    reply = build_reply_text(
        "rename",
        parsed["members"],
        new_name=parsed["new_name"],
        old_name=parsed["old_name"],
        previous={"wxid_a": {"nickname": "小明", "display_name": "旧群昵称"}},
    )
    assert reply == "📋 群成员变动通知:\n📝 昵称修改: 小明 (旧群昵称 -> 新群昵称)"
    assert "未设置" in build_reply_text("rename", parsed["members"], new_name="x")
    # 退出、被移出同样通知
    assert build_reply_text("left", parsed["members"]) == "📋 群成员变动通知:\n❌ 成员减少: 小明"
    assert build_reply_text("removed", parsed["members"]) == "📋 群成员变动通知:\n❌ 成员减少: 小明"


def test_apply_member_change_updates_status_and_nickname():
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.tenant_context import tenant_scope
    from app.core.tenant_db import tenant_session
    from app.models.entities import Chatroom, ChatroomMember

    engine = create_engine("sqlite://")
    for table in (Chatroom, ChatroomMember):
        table.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    channel = SimpleNamespace(id=1, bot_id=1)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=1, external_id="g@chatroom", name="群")
            db.add(room)
            db.flush()
            who = [{"external_id": "wxid_a", "display_name": "小明"}]
            kwargs = dict(channel=channel, chatroom=room, members=who)
            # 新成员加入:建档并置为在群
            apply_member_change(db, change_type="join", **kwargs)
            member = db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "wxid_a"))
            assert member.status == "active" and member.nickname == "小明"
            # 改群昵称:只改 display_name,全局昵称与在群状态不变
            apply_member_change(db, change_type="rename", new_name="新群昵称", **kwargs)
            assert member.display_name == "新群昵称" and member.nickname == "小明" and member.status == "active"
            # 退出:置为 left
            apply_member_change(db, change_type="left", **kwargs)
            assert member.status == "left"
