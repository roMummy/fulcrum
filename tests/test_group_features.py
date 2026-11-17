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
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, TaskJob, WorkWechatContact
from app.services.group_features import dispatch as dispatch_module
from app.services.group_features.dispatch import consumed_command, dispatch_group_features
from app.services.group_features.member_change import build_welcome_cards
from app.services.group_features.registry import all_features, get_feature
from app.services.group_features.store import get_state, is_enabled, save_state
from app.services.group_features.view import build_feature_cards, save_from_form
from app.services.plugin_runtime.replies import MAX_TEXT_LENGTH
from app.services.wechat_webridge.member_directory import apply_event


def test_feature_order_is_independent_of_early_member_parser_import():
    import subprocess
    import sys

    subprocess.run([sys.executable, "-c", (
        "from app.services.group_features import member_change; "
        "from app.services.group_features.registry import all_features; "
        "assert [f.key for f in all_features()] == "
        "['ai_chat', 'inactive_rank', 'media_download', 'member_change', 'red_packet', 'transfer_receive', 'trigger_reply']"
    )], check=True, capture_output=True, text=True)


@pytest.fixture()
def env():
    """一个微桥群 + 几个成员;收集 send_replies 的调用以便断言回复内容。"""

    engine = create_engine("sqlite://")
    for table in (Channel, Conversation, Chatroom, ChatroomMember, TaskJob, WorkWechatContact):
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
    event = _join_event()
    apply_event(env.db, env.channel, event, env.conversation)
    executed = dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
    assert executed == ["member_change"] and not consumed_command(executed)
    replies = sent[0]["replies"]
    assert replies[0]["content"] == "📋 群成员变动通知:\n🆕 成员新增: 小明"
    assert replies[1]["type"] == "link" and replies[1]["title"] == "欢迎 小明" and replies[1]["url"] == "https://example.com/rules"
    assert "{now}" not in replies[1]["description"]
    # 事件型不引用;成员入库且人数被校准
    assert sent[0]["by_command"] is False
    member = env.db.scalar(select(ChatroomMember).where(ChatroomMember.member_external_id == "wxid_a"))
    assert member.status == "active" and env.room.member_count == 9


def test_member_change_uses_only_the_current_groups_templates(env, sent):
    """通过真实群分发选择配置，同账号两个群的文案和群名必须分别生效。"""

    other_room = Chatroom(tenant_id=1, bot_id=1, channel_id=env.channel.id, external_id="other@chatroom", name="二群", is_active=True)
    other_conversation = Conversation(tenant_id=1, bot_id=1, channel_id=env.channel.id, channel_type="wechat_webridge",
                                      external_id=other_room.external_id, chat_type="group")
    env.db.add_all([other_room, other_conversation])
    env.db.flush()
    for room, conversation, wording in [(env.room, env.conversation, "欢迎"), (other_room, other_conversation, "你好")]:
        save_state(room, "member_change", enabled=True, config={
            "notify_title": "{group_name}消息", "join_template": wording + " {nickname}",
        })
        event = _join_event()
        event["conversation"]["external_id"] = room.external_id
        apply_event(env.db, env.channel, event, conversation)
        assert dispatch_group_features(env.db, env.channel, event, conversation_id=conversation.id) == ["member_change"]
    assert [call["replies"] for call in sent] == [
        [{"type": "text", "content": "群消息\n欢迎 小明"}],
        [{"type": "text", "content": "二群消息\n你好 小明"}],
    ]


@pytest.mark.parametrize(("change_type", "template_key", "template", "expected"), [
    ("left", "leave_template", "再见 {nickname} · {group_name}", "再见 小明 · 群"),
    ("removed", "removed_template", "{group_name} 已移出 {nickname}", "群 已移出 小明"),
    ("rename", "rename_template", "{nickname}：{old_nickname} → {new_nickname}", "全局昵称：事件旧昵称 → 新群昵称"),
])
def test_non_join_templates_consume_the_inbound_directory_context(env, sent, change_type, template_key, template, expected):
    """真实入站先更新目录，异步通知仍消费保存的旧资料，不能把新昵称误作用户昵称或旧昵称。"""

    member = ChatroomMember(tenant_id=1, bot_id=1, channel_id=env.channel.id, chatroom_id=env.room.id,
                           member_external_id="wxid_a", nickname="全局昵称", display_name="目录旧昵称", status="active")
    env.db.add(member)
    env.db.flush()
    save_state(env.room, "member_change", enabled=True, config={"notify_title": "", template_key: template})
    event = _join_event()
    event["message"]["message_type"] = "group_member_nickname_change" if change_type == "rename" else "group_member_leave"
    event["details"]["member_change"].update(
        change_type=change_type, old_room_nick_name="事件旧昵称", new_room_nick_name="新群昵称",
    )
    apply_event(env.db, env.channel, event, env.conversation)
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == ["member_change"]
    assert sent[0]["replies"] == [{"type": "text", "content": expected}]
    if change_type == "rename":
        assert member.display_name == "新群昵称" and member.nickname == "全局昵称"
    else:
        assert member.status == "left"


@pytest.mark.parametrize(("title", "expected"), [
    ("{group_name}：{nickname}", "群：小明、小红\n欢迎 小明\n请阅读群公告\n欢迎 小红\n请阅读群公告"),
    ("", "欢迎 小明\n请阅读群公告\n欢迎 小红\n请阅读群公告"),
])
def test_member_change_multiple_members_share_one_message(env, sent, monkeypatch, title, expected):
    """多人正文逐人替换后合并，标题仅一份，纯文字通知不增加头像目录查询。"""

    from app.services.identity_directory import IdentityDirectory

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("文字文案不应查询头像目录")

    monkeypatch.setattr(IdentityDirectory, "preload", unexpected_lookup)
    save_state(env.room, "member_change", enabled=True, config={
        "notify_title": title, "join_template": "欢迎 {nickname}\n请阅读群公告",
    })
    event = _join_event()
    event["details"]["member_change"]["members"].append({"external_id": "wxid_b", "display_name": "小红"})
    apply_event(env.db, env.channel, event, env.conversation)
    dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
    assert len(sent) == 1 and sent[0]["replies"] == [{"type": "text", "content": expected}]


@pytest.mark.parametrize("card_enabled", [False, True])
def test_empty_join_text_keeps_welcome_cards_independent(env, sent, card_enabled):
    """加入正文留空时不发送孤立标题，卡片仍由既有卡片开关控制。"""

    save_state(env.room, "member_change", enabled=True, config={
        "notify_title": "不应单独发送", "join_template": "", "card_enabled": card_enabled,
        "card_title": "欢迎 {nickname}", "card_url": "https://example.com/rules",
    })
    event = _join_event()
    apply_event(env.db, env.channel, event, env.conversation)
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == ["member_change"]
    if card_enabled:
        assert len(sent) == 1 and [reply["type"] for reply in sent[0]["replies"]] == ["link"]
        assert sent[0]["replies"][0]["title"] == "欢迎 小明"
    else:
        assert sent == []


def test_long_notification_is_bounded_without_losing_welcome_cards(env, sent):
    """多人长文案明确提示截断并限制为一条文字，不能因超限异常丢掉整次欢迎卡片。"""

    save_state(env.room, "member_change", enabled=True, config={
        "join_template": "欢迎 {nickname}\n" + "群规" * 900,
        "card_enabled": True, "card_title": "欢迎 {nickname}", "card_url": "https://example.com/rules",
    })
    event = _join_event()
    event["details"]["member_change"]["members"] = [
        {"external_id": f"wxid_{i}", "display_name": f"成员{i}"} for i in range(15)
    ]
    apply_event(env.db, env.channel, event, env.conversation)
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == ["member_change"]
    replies = sent[0]["replies"]
    assert [reply["type"] for reply in replies] == ["text", "link", "link", "link"]
    assert len(replies[0]["content"]) <= MAX_TEXT_LENGTH
    assert replies[0]["content"].endswith("…（通知内容过长，已截断）")


def test_member_change_switches_and_bad_card_url(env, sent):
    # 关掉加入通知:仍同步成员,但不发消息
    save_state(env.room, "member_change", enabled=True, config={"notify_join": False})
    event = _join_event("小红")
    apply_event(env.db, env.channel, event, env.conversation)
    dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
    assert sent == []
    assert env.db.scalar(select(ChatroomMember).where(ChatroomMember.display_name == "小红")) is not None
    # 卡片地址不是 http(s) 网址:跳过卡片,文字通知照发
    save_state(env.room, "member_change", enabled=True, config={"card_enabled": True, "card_url": "不是网址"})
    event = _join_event("小刚")
    apply_event(env.db, env.channel, event, env.conversation)
    dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
    assert [item["type"] for item in sent[0]["replies"]] == ["text"]
    # 普通聊天文本与本能力无关:不执行
    sent.clear()
    assert dispatch_group_features(env.db, env.channel, _text_event("大家好"), conversation_id=env.conversation.id) == []


def test_welcome_card_avatar_matches_each_new_member_and_current_group(env, sent):
    """多人入群时逐人取本群头像，缺失时取同渠道好友头像，不误用邀请人的头像。"""

    own_avatar = "https://wx.qlogo.cn/mmhead/member-a/132"
    friend_avatar = "https://wx.qlogo.cn/mmhead/member-b/132"
    env.db.add_all([
        ChatroomMember(tenant_id=1, bot_id=1, channel_id=env.channel.id, chatroom_id=env.room.id,
                       member_external_id="wxid_a", extra_json=json.dumps({"avatar_url": own_avatar})),
        WorkWechatContact(tenant_id=1, channel_id=env.channel.id, contact_external_id="wxid_a",
                         avatar_url="https://example.com/older-friend-avatar.jpg"),
        WorkWechatContact(tenant_id=1, channel_id=env.channel.id, contact_external_id="wxid_b", avatar_url=friend_avatar),
        WorkWechatContact(tenant_id=1, channel_id=env.channel.id, contact_external_id="wxid_inviter",
                         avatar_url="https://example.com/inviter.jpg"),
    ])
    env.db.flush()
    save_state(env.room, "member_change", enabled=True, config={
        "card_enabled": True, "card_title": "欢迎 {nickname}", "card_url": "https://example.com/rules",
        # 仅靠来源选择触发头像读取，不能要求用户再填写变量；保留的自定义 URL 不应生效。
        "card_thumb_source": "member_avatar", "card_thumb_url": "https://example.com/custom.jpg",
    })
    event = _join_event()
    event["message"]["sender_id"] = "wxid_inviter"
    event["details"]["member_change"]["members"].append({"external_id": "wxid_b", "display_name": "小红"})
    apply_event(env.db, env.channel, event, env.conversation)
    dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
    cards = [reply for reply in sent[0]["replies"] if reply["type"] == "link"]
    assert [(card["title"], card["thumb_url"]) for card in cards] == [("欢迎 小明", own_avatar), ("欢迎 小红", friend_avatar)]
    # 补充头像只作用于卡片上下文，不能修改入站事件中的原始证据。
    assert all("avatar_url" not in item for item in event["details"]["member_change"]["members"])


def test_welcome_card_avatar_does_not_use_another_group_or_channel(env, sent):
    """相同 wxid 在别群或别渠道的资料不能补进当前新成员卡片。"""

    other_room = Chatroom(tenant_id=1, bot_id=1, channel_id=env.channel.id, external_id="other@chatroom", name="其他群")
    env.db.add(other_room)
    env.db.flush()
    env.db.add_all([
        ChatroomMember(tenant_id=1, bot_id=1, channel_id=env.channel.id, chatroom_id=other_room.id,
                       member_external_id="wxid_a", extra_json=json.dumps({"avatar_url": "https://example.com/other-group.jpg"})),
        WorkWechatContact(tenant_id=1, channel_id=env.channel.id + 100, contact_external_id="wxid_a",
                         avatar_url="https://example.com/other-channel.jpg"),
    ])
    env.db.flush()
    save_state(env.room, "member_change", enabled=True, config={
        "card_enabled": True, "card_url": "https://example.com/rules", "card_thumb_source": "member_avatar",
    })
    event = _join_event()
    apply_event(env.db, env.channel, event, env.conversation)
    dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
    cards = [reply for reply in sent[0]["replies"] if reply["type"] == "link"]
    assert len(cards) == 1 and "thumb_url" not in cards[0]


@pytest.mark.parametrize("avatar", ["", "file:///private/avatar.png", "https://user:secret@example.com/avatar.png", "https://["])
def test_welcome_card_missing_or_invalid_avatar_still_sends_each_card(avatar):
    """头像缺失或不合法时省略缩略图，后续新成员仍能收到各自的欢迎卡片。"""

    cards = build_welcome_cards({"card_url": "https://example.com/rules", "card_title": "欢迎 {nickname}", "card_thumb_source": "member_avatar"}, [
        {"external_id": "a", "display_name": "小明", "avatar_url": avatar},
        {"external_id": "b", "display_name": "小红", "avatar_url": "https://example.com/b.jpg"},
    ])
    assert len(cards) == 2 and "thumb_url" not in cards[0]
    assert cards[1]["thumb_url"] == "https://example.com/b.jpg"


def test_welcome_card_templates_replace_variables_once_and_keep_custom_urls():
    """所有卡片字段使用同一轮替换；昵称里的变量外观及用户自定义 URL 不应被再次解释。"""

    avatar = "https://wx.qlogo.cn/mmhead/member/132"
    member = {"external_id": "a", "display_name": "小明{avatar_url}{now}", "avatar_url": avatar}
    config = {"card_title": "欢迎 {nickname} {unknown}", "card_desc": "{now}",
              "card_url": "{avatar_url}", "card_thumb_source": "member_avatar"}
    card = build_welcome_cards(config, [member])[0]
    assert card["title"] == "欢迎 小明{avatar_url}{now} {unknown}"
    assert "{now}" not in card["description"]
    assert card["url"] == avatar and card["thumb_url"] == avatar
    config.update(card_url="https://example.com/rules", card_thumb_source="url", card_thumb_url="https://example.com/custom-cover.jpg")
    assert build_welcome_cards(config, [member])[0]["thumb_url"] == "https://example.com/custom-cover.jpg"


@pytest.mark.parametrize(("source", "avatar", "expected"), [
    ("url", "https://example.com/member.jpg", "https://example.com/custom.jpg"),
    ("member_avatar", "https://example.com/member.jpg", "https://example.com/member.jpg"),
    ("member_avatar", "", None),
])
def test_welcome_card_thumbnail_uses_only_selected_source(source, avatar, expected):
    """来源互斥：头像模式不能使用已保留的自定义 URL，头像缺失也不能隐式改用该地址。"""

    cards = build_welcome_cards({
        "card_url": "https://example.com/rules", "card_title": "欢迎 {nickname}",
        "card_thumb_source": source, "card_thumb_url": "https://example.com/custom.jpg",
    }, [{"external_id": "a", "display_name": "小明", "avatar_url": avatar}])
    assert len(cards) == 1
    assert cards[0].get("thumb_url") == expected


@pytest.mark.parametrize(("source", "expected"), [("url", "url"), ("member_avatar", "member_avatar"), ("invalid", "url")])
def test_welcome_card_image_source_roundtrip_and_option_validation(env, source, expected):
    """配置只接受声明选项，保存头像来源时仍保留自定义 URL，便于日后切回。"""

    save_state(env.room, "member_change", enabled=True, config={
        "card_thumb_source": source, "card_thumb_url": "https://example.com/custom.jpg",
    })
    _, config = get_state(env.room, get_feature("member_change"))
    assert config["card_thumb_source"] == expected
    assert config["card_thumb_url"] == "https://example.com/custom.jpg"


def test_welcome_card_without_avatar_variable_does_not_query_identity(env, sent, monkeypatch):
    """未使用头像变量时不读取身份目录，不增加已有纯文字/自定义封面卡片的查询成本。"""

    from app.services.identity_directory import IdentityDirectory

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError("未使用头像变量的欢迎卡片不应查询身份目录")

    monkeypatch.setattr(IdentityDirectory, "preload", unexpected_lookup)
    save_state(env.room, "member_change", enabled=True, config={
        "card_enabled": True, "card_url": "https://example.com/rules",
        "card_thumb_source": "url", "card_thumb_url": "https://example.com/cover.jpg",
    })
    event = _join_event()
    apply_event(env.db, env.channel, event, env.conversation)
    dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
    assert sent[0]["replies"][1]["thumb_url"] == "https://example.com/cover.jpg"
