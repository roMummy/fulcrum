"""微桥群成员事件(加入/离开/改群昵称)的适配与群功能处理轻量测试。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="member-events-logs-"))

from types import SimpleNamespace

import pytest

from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.group_features import member_change
from app.services.group_features.member_change import FEATURE, apply_member_change, build_reply_text, parse_member_change


def _notification(change_type, members, *, config=None, group_name="测试群", **kwargs):
    """使用字段声明提供默认值，纯函数与群配置读取共用同一份产品默认文案。"""

    settings = {field.key: field.default for field in FEATURE.fields}
    settings.update(config or {})
    return build_reply_text(change_type, members, config=settings, group_name=group_name, **kwargs)


def _normalize(event_name: str, **body):
    adapter = WechatWeBridgeAdapter()
    base = {"room_id": "g@chatroom", "is_group": True, "member_id": "wxid_a", "member_display_name": "小明", "snapshot_after_count": 24}
    return adapter.normalize_inbound_payload(SimpleNamespace(id=2, runtime_info_json="{}"), {"event_name": event_name, "body": {**base, **body}})


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
    assert _notification("join", parsed["members"]) == "📋 群成员变动通知:\n🆕 成员新增: 小明"


def test_nickname_change_event():
    event = _normalize("group_member_nickname_change", old_room_nick_name="旧群昵称", new_room_nick_name="新群昵称")
    assert event["message"]["message_type"] == "group_member_nickname_change"
    parsed = parse_member_change(event)
    assert parsed["change_type"] == "rename" and parsed["new_name"] == "新群昵称"
    assert parsed["old_name"] == "旧群昵称"
    # 改群昵称发变动通知:优先保留事件里的旧昵称,缺失时使用改名前的目录资料。
    reply = _notification(
        "rename",
        parsed["members"],
        new_name=parsed["new_name"],
        old_name=parsed["old_name"],
        previous={"wxid_a": {"nickname": "小明", "display_name": "旧群昵称"}},
    )
    assert reply == "📋 群成员变动通知:\n📝 昵称修改: 小明 (旧群昵称 -> 新群昵称)"
    assert "未设置" in _notification("rename", parsed["members"], new_name="x")
    # 退出、被移出同样通知
    assert _notification("left", parsed["members"]) == "📋 群成员变动通知:\n❌ 成员减少: 小明"
    assert _notification("removed", parsed["members"]) == "📋 群成员变动通知:\n❌ 成员减少: 小明"


@pytest.mark.parametrize(("change_type", "template_key", "verb"), [
    ("join", "join_template", "欢迎"),
    ("left", "leave_template", "再见"),
    ("removed", "removed_template", "已移出"),
    ("rename", "rename_template", "昵称更新"),
])
def test_each_change_uses_its_group_template_and_context(monkeypatch, change_type, template_key, verb):
    """四种事件选择各自正文，并提供成员昵称、当前群名和同一事件时间。"""

    from datetime import datetime

    monkeypatch.setattr(member_change, "now_shanghai", lambda: datetime(2026, 11, 15, 20, 30))
    reply = _notification(change_type, [{"external_id": "wxid_a", "display_name": "小明"}], config={
        "notify_title": "{group_name}：{nickname}",
        template_key: f"{verb} {{nickname}}\n{{group_name}} · {{now}}",
    })
    assert reply == f"测试群：小明\n{verb} 小明\n测试群 · 2026-11-15 20:30"


@pytest.mark.parametrize(("old_name", "previous", "new_name", "expected"), [
    ("事件旧昵称", {"wxid_a": {"nickname": "小明", "display_name": "目录旧昵称"}}, "新群昵称", "小明：事件旧昵称 → 新群昵称"),
    ("", {"wxid_a": {"nickname": "小明", "display_name": "目录旧昵称"}}, "", "小明：目录旧昵称 → 已清除"),
    ("", {"wxid_a": {"nickname": "小明"}}, "新群昵称", "小明：小明 → 新群昵称"),
    ("", {}, "新群昵称", "事件称呼：未设置 → 新群昵称"),
])
def test_rename_variables_use_pre_change_identity(old_name, previous, new_name, expected):
    """昵称模板沿用事件证据优先的旧昵称，用户昵称取改名前目录，清除时给出明确提示。"""

    assert _notification("rename", [{"external_id": "wxid_a", "display_name": "事件称呼"}], config={
        "notify_title": "", "rename_template": "{nickname}：{old_nickname} → {new_nickname}",
    }, old_name=old_name, previous=previous, new_name=new_name) == expected


def test_templates_replace_once_and_keep_unknown_variables():
    """昵称及群名可以含花括号；替换结果不能作为模板继续执行，未知变量原样保留。"""

    name = "小明{group_name}{now}"
    group = "群{nickname}"
    assert _notification("join", [{"external_id": "wxid_a", "display_name": name}], config={
        "notify_title": "{group_name}：{nickname}",
        "join_template": "{nickname} @ {group_name} {unknown} {old_nickname}",
    }, group_name=group) == f"{group}：{name}\n{name} @ {group} {{unknown}} {{old_nickname}}"


@pytest.mark.parametrize("template_key", ["join_template", "leave_template", "removed_template", "rename_template"])
def test_empty_body_never_sends_a_standalone_title(template_key):
    """各类正文留空均停发该类文字，非空标题不能单独形成无意义通知。"""

    change_type = {"join_template": "join", "leave_template": "left", "removed_template": "removed", "rename_template": "rename"}[template_key]
    assert _notification(change_type, [{"external_id": "wxid_a", "display_name": "小明"}], config={
        "notify_title": "仍有标题", template_key: " \n ",
    }) == ""


def test_notification_uses_member_id_when_nickname_is_missing():
    """缺少昵称时只使用明确的成员 ID，避免生成空人名通知。"""

    assert _notification("join", [{"external_id": "wxid_a"}], config={"notify_title": ""}) == "🆕 成员新增: wxid_a"


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
