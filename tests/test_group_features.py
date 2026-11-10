"""群功能(成员变更提醒、潜水排行)的轻量测试:配置存取、表单保存、分发入口与各能力处理。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="group-features-logs-"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation
from app.services.group_features import dispatch as dispatch_module
from app.services.group_features.dispatch import consumed_command, dispatch_group_features
from app.services.group_features.registry import all_features, get_feature
from app.services.group_features.store import get_state, is_enabled, save_state
from app.services.group_features.view import build_feature_cards, save_from_form


@pytest.fixture()
def env():
    """一个微桥群 + 几个成员;收集 send_replies 的调用以便断言回复内容。"""

    engine = create_engine("sqlite://")
    for table in (Channel, Conversation, Chatroom, ChatroomMember):
        table.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge", status="online", is_enabled=True)
            db.add(channel)
            db.flush()
            conversation = Conversation(
                tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge",
                external_id="g@chatroom", chat_type="group",
            )
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="群", is_active=True)
            db.add_all([conversation, room])
            db.flush()
            current = now_shanghai()
            db.add_all(
                [
                    ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="never", nickname="从未发言", status="active"),
                    ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="old", nickname="老潜水", status="active", last_active_at=current - timedelta(days=45)),
                    ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="new", nickname="活跃", status="active", last_active_at=current - timedelta(days=1)),
                    ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="gone", nickname="已退群", status="left"),
                ]
            )
            db.flush()
            yield SimpleNamespace(db=db, channel=channel, conversation=conversation, room=room)


@pytest.fixture()
def sent(monkeypatch):
    calls: list[dict] = []

    def fake_send(db, channel, conversation, replies, **kwargs):
        calls.append({"replies": replies, **kwargs})

    monkeypatch.setattr(dispatch_module, "send_replies", fake_send)
    return calls


def _text_event(content: str) -> dict:
    return {
        "conversation": {"chat_type": "group", "external_id": "g@chatroom"},
        "message": {"message_type": "text", "content": content, "sender_id": "wxid_x"},
    }


def _join_event(name: str = "小明") -> dict:
    return {
        "conversation": {"chat_type": "group", "external_id": "g@chatroom"},
        "message": {"message_type": "group_member_join", "content": f"{name} 加入了群聊"},
        "details": {"member_change": {"change_type": "join", "members": [{"external_id": "wxid_a", "display_name": name}], "after_count": 9}},
    }


def test_registry_has_both_features():
    assert {item.key for item in all_features()} >= {"member_change", "inactive_rank"}
    assert get_feature("inactive_rank").commands == ("潜水排行",)


def test_store_defaults_off_and_roundtrip(env):
    room = env.room
    assert not is_enabled(room, "inactive_rank")
    save_state(room, "inactive_rank", enabled=True, config={"default_days": "30", "default_limit": 9999})
    enabled, config = get_state(room, get_feature("inactive_rank"))
    # 数字配置按范围钳制:名数最大 50
    assert enabled and config == {"default_days": 30, "default_limit": 50}
    assert is_enabled(room, "inactive_rank")
    # 只改开关不丢配置;不动 extra_json 里的其他键
    room.extra_json = json.dumps({**json.loads(room.extra_json), "auto_download_media": True})
    save_state(room, "inactive_rank", enabled=False)
    assert get_state(room, get_feature("inactive_rank")) == (False, {"default_days": 30, "default_limit": 50})
    assert json.loads(room.extra_json)["auto_download_media"] is True
    # 群的“启用”是总开关:群未启用时,即使打开了能力开关也视为未启用
    save_state(room, "inactive_rank", enabled=True)
    assert is_enabled(room, "inactive_rank")
    room.is_active = False
    assert not is_enabled(room, "inactive_rank")


def test_save_from_form_checkbox_semantics(env):
    room = env.room
    message = save_from_form(room, "member_change", {"enabled": "true", "cfg_notify_join": "true", "cfg_card_title": "你好 {nickname}"})
    assert "已开启" in message
    enabled, config = get_state(room, get_feature("member_change"))
    # 没提交的复选框视为关闭
    assert enabled and config["notify_join"] is True and config["notify_leave"] is False and config["card_title"] == "你好 {nickname}"
    cards = {item["key"]: item for item in build_feature_cards(room)}
    assert cards["member_change"]["enabled"] is True
    with pytest.raises(ValueError):
        save_from_form(room, "not_exist", {})


def test_dispatch_obeys_group_master_switch(env, sent):
    event = _text_event("潜水排行")
    # 默认关闭:不执行
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == []
    assert sent == []
    # 能力开关打开但群未启用:不执行
    save_state(env.room, "inactive_rank", enabled=True)
    env.room.is_active = False
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == []
    assert sent == []
    # 群启用后才执行
    env.room.is_active = True
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == ["inactive_rank"]


def test_inactive_rank_command_replies_and_blocks_ai(env, sent):
    save_state(env.room, "inactive_rank", enabled=True, config={"default_days": 30, "default_limit": 10})
    executed = dispatch_group_features(env.db, env.channel, _text_event("潜水排行"), conversation_id=env.conversation.id)
    assert executed == ["inactive_rank"] and consumed_command(executed)
    text = sent[0]["replies"][0]["content"]
    lines = text.splitlines()
    # 从未发言排最前,已退群和近期活跃的不出现
    assert lines[1].startswith("1. 从未发言") and "暂无发言记录" in lines[1]
    assert lines[2].startswith("2. 老潜水") and "最后发言 45 天前" in lines[2]
    assert "已退群" not in text and "活跃" not in text
    # 命令型按真实消息回复(可引用触发消息)
    assert sent[0]["by_command"] is True
    # 带 / 前缀和参数;非法参数给出格式提示;普通聊天不触发
    sent.clear()
    dispatch_group_features(env.db, env.channel, _text_event("/潜水排行 30 1"), conversation_id=env.conversation.id)
    assert "前 1 名" in sent[0]["replies"][0]["content"]
    sent.clear()
    dispatch_group_features(env.db, env.channel, _text_event("潜水排行 abc"), conversation_id=env.conversation.id)
    assert "命令格式" in sent[0]["replies"][0]["content"]
    sent.clear()
    assert dispatch_group_features(env.db, env.channel, _text_event("今天潜水排行怎么样"), conversation_id=env.conversation.id) == []


def test_member_change_join_notifies_syncs_and_sends_card(env, sent):
    save_state(
        env.room, "member_change", enabled=True,
        config={"card_enabled": True, "card_title": "欢迎 {nickname}", "card_desc": "到点了 {now}", "card_url": "https://example.com/rules"},
    )
    executed = dispatch_group_features(env.db, env.channel, _join_event(), conversation_id=env.conversation.id)
    assert executed == ["member_change"] and not consumed_command(executed)
    replies = sent[0]["replies"]
    assert replies[0]["content"] == "📋 群成员变动通知:\n🆕 成员新增: 小明"
    assert replies[1]["type"] == "link" and replies[1]["title"] == "欢迎 小明" and replies[1]["url"] == "https://example.com/rules"
    assert "{now}" not in replies[1]["description"]
    # 事件型不引用;成员入库且人数被校准
    assert sent[0]["by_command"] is False
    member = env.db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "wxid_a"))
    assert member.status == "active" and env.room.member_count == 9


def test_member_change_switches_and_bad_card_url(env, sent):
    # 关掉加入通知:仍同步成员,但不发消息
    save_state(env.room, "member_change", enabled=True, config={"notify_join": False})
    dispatch_group_features(env.db, env.channel, _join_event("小红"), conversation_id=env.conversation.id)
    assert sent == []
    assert env.db.scalar(select(ChatroomMember).where(ChatroomMember.display_name == "小红")) is not None
    # 卡片地址不是 http(s) 网址:跳过卡片,文字通知照发
    save_state(env.room, "member_change", enabled=True, config={"card_enabled": True, "card_url": "不是网址"})
    dispatch_group_features(env.db, env.channel, _join_event("小刚"), conversation_id=env.conversation.id)
    assert [item["type"] for item in sent[0]["replies"]] == ["text"]
    # 普通聊天文本与本能力无关:不执行
    sent.clear()
    assert dispatch_group_features(env.db, env.channel, _text_event("大家好"), conversation_id=env.conversation.id) == []
