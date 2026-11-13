"""群级触发回复:拍一拍/关键词触发、文字/图片/语音/视频/卡片回复、冷却与随机、素材安全、管理页面。"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="trigger-rules-logs-"))

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.web.group_triggers as group_triggers_web
from app.core.settings import settings
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation
from app.models.plugin_business import GroupTriggerRule
from app.services.group_features import dispatch as dispatch_module
from app.services.group_features.dispatch import dispatch_group_features
from app.services.group_features.store import save_state
from app.services.trigger_rules import engine, media
from app.services.trigger_rules.catalog import COMMON_VARIABLES, EventView, all_triggers, get_trigger, parse_pat, trigger_event_types, unknown_placeholders, variables_of
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
MP3 = b"ID3" + b"\x00" * 64


@pytest.fixture(autouse=True)
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


def _view(message_type: str = "pat", text: str = "", **kwargs) -> EventView:
    return EventView(message_type=message_type, text=text, sender_id=kwargs.pop("sender_id", "wxid_a"), sender_name=kwargs.pop("sender_name", "小明"), group_name="测试群",
                     details=kwargs.pop("details", {}), names=kwargs.pop("names", {}))


# 适配器按协议 4.3.3 的结构化字段整理出的拍一拍(拍的人/被拍的人是 wxid),触发器不看文案
PAT_BOT = {"pat": {"from_id": "wxid_red", "patted_id": "wxid_bot", "patted_is_me": True}}
PAT_OTHER = {"pat": {"from_id": "wxid_red", "patted_id": "wxid_other", "patted_is_me": False}}
NAMES = {"wxid_red": "小红", "wxid_other": "小刚"}


def _pat_me() -> EventView:
    return _view(details=PAT_BOT, names=NAMES)


def _pat_other() -> EventView:
    return _view(details=PAT_OTHER, names=NAMES)


# ---------- 拍一拍解析:谁拍了谁 ----------

def test_pat_is_read_from_the_structured_ids_and_never_from_the_text() -> None:
    mine, other = parse_pat(_pat_me()), parse_pat(_pat_other())
    assert (mine.from_id, mine.patted_id, mine.patted_is_me) == ("wxid_red", "wxid_bot", True)
    assert (other.from_id, other.patted_id, other.patted_is_me) == ("wxid_red", "wxid_other", False)
    # 适配器没有给出结构化字段:不是拍一拍,哪怕文案长得一模一样
    assert parse_pat(_view(text='"小红" 拍了拍 我')) is None
    assert parse_pat(_view(text='"小红" 拍了拍 "小机@bot"', details={})) is None
    # 不是 pat 事件:即使带了 details.pat 也不当拍一拍
    assert parse_pat(_view("text", details=PAT_BOT)) is None


def test_pat_trigger_scope_me_only_reacts_when_the_bot_is_patted() -> None:
    pat = get_trigger("pat")
    assert pat.match({"scope": "me"}, _pat_me()) == {"sender_name": "小红", "patted_name": "我"}
    assert pat.match({"scope": "me"}, _pat_other()) is None
    assert pat.match({"scope": "any"}, _pat_other()) == {"sender_name": "小红", "patted_name": "小刚"}
    # 缺 ID 或本账号 ID 未知(适配器没给 patted_is_me):判断不了,"只回应拍机器人"不触发,宁可不回也不乱回
    unknown = _view(details={"pat": {"from_id": "wxid_red", "patted_id": "wxid_bot"}}, names=NAMES)
    assert pat.match({"scope": "me"}, unknown) is None and pat.match({"scope": "any"}, unknown) is not None
    assert pat.match({"scope": "me"}, _view("text", '"小红" 拍了拍 我')) is None  # 普通文字不算拍一拍事件


def test_names_only_fill_the_variables_and_fall_back_without_leaking_wxids() -> None:
    pat = get_trigger("pat")
    nameless = _view(details=PAT_BOT)  # 群成员表里一个名字都没有
    result = pat.match({"scope": "me"}, nameless)
    assert result["patted_name"] == "我"            # 机器人自己没有昵称时用"我",不把 wxid 发到群里
    assert result["sender_name"] == "wxid_red"      # 拍的人查不到名字时只能退回 ID(这是已知的取舍)
    assert pat.match({"scope": "me"}, _view(details=PAT_BOT, names={"wxid_bot": "小机@bot", "wxid_red": "小红"}))["patted_name"] == "小机@bot"


def test_keyword_trigger_modes() -> None:
    keyword = get_trigger("keyword")
    text = lambda content: _view("text", content)
    assert keyword.match({"text": "早安", "mode": "contains"}, text("大家早安呀")) is not None
    assert keyword.match({"text": "早安", "mode": "exact"}, text("大家早安呀")) is None
    assert keyword.match({"text": "早安", "mode": "exact"}, text("早安")) is not None
    assert keyword.match({"text": "查", "mode": "prefix"}, text("查 北京"))["args"] == "北京"
    assert keyword.match({"text": "", "mode": "contains"}, text("x")) is None
    assert {"pat", "text", "group_member_join"} <= set(trigger_event_types())


# ---------- 素材安全 ----------

def test_uploads_are_checked_by_extension_size_and_file_header() -> None:
    assert media.resolve(1, media.save_upload(1, "image", "a.PNG", _png()), "image").is_file()
    assert media.save_upload(1, "video", "v.mp4", MP4).endswith(".mp4")
    assert media.save_upload(1, "audio", "s.mp3", MP3).endswith(".mp3")
    for kind, name, data, expected in (
        ("image", "a.exe", _png(), "格式不支持"),
        ("image", "a.png", b"not an image", "不是有效"),
        ("video", "v.mp4", b"x" * 64, "不是有效"),
        ("audio", "s.mp3", b"", "空的"),
        ("audio", "s.mp3", b"x" * (5 * 1024 * 1024 + 1), "不能超过"),
    ):
        with pytest.raises(media.MediaError, match=expected):
            media.save_upload(1, kind, name, data)


def test_stored_names_cannot_escape_the_tenant_directory_or_cross_tenants() -> None:
    stored = media.save_upload(1, "image", "a.png", _png())
    for bad in ("../../etc/passwd", "/etc/passwd", "a.png", stored.replace(".png", ".exe"), ""):
        with pytest.raises(media.MediaError):
            media.resolve(1, bad, "image")
    with pytest.raises(media.MediaError):
        media.resolve(2, stored, "image")  # 别的租户读不到
    with pytest.raises(media.MediaError):
        media.resolve(1, stored, "video")  # 类型不符
    media.delete(1, "../../etc/passwd")  # 非法文件名被忽略,不会删任何东西


# ---------- 规则校验与保存 ----------

def _draft(**overrides) -> engine.RuleDraft:
    base = dict(name="拍一拍", trigger_type="pat", trigger_params={"scope": "me"}, actions=[{"type": "text", "content": "别拍啦,{sender_name}"}], pick_mode="all", cooldown_seconds=0)
    base.update(overrides)
    return engine.RuleDraft(**base)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"name": "  "}, "起个名字"),
        ({"trigger_type": "nope"}, "触发条件"),
        ({"actions": []}, "至少添加一条回复"),
        ({"actions": [{"type": "text", "content": ""}]}, "请填写「回复内容」"),
        ({"actions": [{"type": "card", "title": "t", "url": "javascript:alert(1)"}]}, "http://"),
        ({"actions": [{"type": "image", "file": "../x.png"}]}, "无效"),
        ({"actions": [{"type": "text", "content": "x"}] * 7}, "最多 6 条"),
        ({"pick_mode": "weird"}, "回复方式"),
        ({"trigger_type": "keyword", "trigger_params": {"text": "", "mode": "contains"}}, "请填写「关键词」"),
    ],
)
def test_incomplete_rules_get_a_readable_message(overrides, expected) -> None:
    with pytest.raises(engine.RuleError, match=expected):
        engine.validate_draft(_draft(**overrides), tenant_id=1)


@pytest.fixture()
def env():
    engine_ = create_engine("sqlite://")
    for table in (Channel, Conversation, Chatroom, ChatroomMember, GroupTriggerRule):
        table.__table__.create(engine_)
    factory = sessionmaker(bind=engine_, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge", status="online", is_enabled=True)
            db.add(channel)
            db.flush()
            conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group")
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="测试群", is_active=True)
            db.add_all([conversation, room])
            db.flush()
            yield SimpleNamespace(db=db, channel=channel, conversation=conversation, room=room)


def _run(env, view: EventView):
    return engine.execute_rules(env.db, tenant_id=1, chatroom_id=env.room.id, view=view)


def test_text_reply_fills_in_variables(env) -> None:
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft())
    replies = _run(env, _pat_me())
    assert replies == [{"type": "text", "content": "别拍啦,小红"}]
    assert _run(env, _pat_other()) is None  # 拍的不是机器人:没有规则命中


def test_every_kind_of_reply_can_be_built(env) -> None:
    image, voice, video = (media.save_upload(1, "image", "a.png", _png()), media.save_upload(1, "audio", "s.mp3", MP3), media.save_upload(1, "video", "v.mp4", MP4))
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(actions=[
        {"type": "text", "content": "hi"},
        {"type": "image", "file": image},
        {"type": "voice", "file": voice},
        {"type": "video", "file": video, "cover": ""},
        {"type": "card", "title": "{group} 欢迎 {sender_name}", "description": "{time}", "url": "https://example.com/x", "thumb_url": ""},
    ]))
    text, img, aud, vid, card = _run(env, _pat_me())
    assert text["type"] == "text" and img["type"] == "image" and img["image_path"].endswith(".png")
    assert aud["type"] == "voice" and base64.b64decode(aud["base64"]) == MP3
    assert vid["type"] == "video" and base64.b64decode(vid["base64"]) == MP4 and vid["image_base64"] == media.PLACEHOLDER_COVER_BASE64  # 没传封面用默认封面
    assert card["type"] == "link" and card["title"] == "测试群 欢迎 小红" and card["url"] == "https://example.com/x"


def test_random_mode_sends_exactly_one_and_cooldown_stops_repeats(env) -> None:
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(
        pick_mode="random", cooldown_seconds=60,
        actions=[{"type": "text", "content": "甲"}, {"type": "text", "content": "乙"}, {"type": "text", "content": "丙"}],
    ))
    first = _run(env, _pat_me())
    assert len(first) == 1 and first[0]["content"] in {"甲", "乙", "丙"}
    assert _run(env, _pat_me()) == []  # 冷却中:命中但不回复
    rule = env.db.scalar(select(GroupTriggerRule))
    rule.last_triggered_at = now_shanghai().replace(year=2020)
    assert len(_run(env, _pat_me())) == 1  # 冷却过了又能触发


def test_disabled_rules_are_skipped_and_the_first_matching_rule_wins(env) -> None:
    first = engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(name="甲", actions=[{"type": "text", "content": "甲规则"}]))
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(name="乙", actions=[{"type": "text", "content": "乙规则"}]))
    assert _run(env, _pat_me())[0]["content"] == "甲规则"  # 同一件事只回一次
    first.is_enabled = False
    assert _run(env, _pat_me())[0]["content"] == "乙规则"


def test_a_missing_media_file_skips_only_that_reply(env) -> None:
    image = media.save_upload(1, "image", "a.png", _png())
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(actions=[{"type": "image", "file": image}, {"type": "text", "content": "还在"}]))
    media.delete(1, image)  # 文件被人删了
    assert _run(env, _pat_me()) == [{"type": "text", "content": "还在"}]


def test_editing_and_deleting_a_rule_cleans_up_unused_files(env) -> None:
    old = media.save_upload(1, "image", "a.png", _png())
    rule = engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(actions=[{"type": "image", "file": old}]))
    new = media.save_upload(1, "image", "b.png", _png())
    engine.update_rule(env.db, rule, _draft(actions=[{"type": "image", "file": new}]))
    assert not (media.media_root(1) / old).exists() and (media.media_root(1) / new).exists()
    engine.delete_rule(env.db, rule)
    assert not (media.media_root(1) / new).exists()


# ---------- 变量:每种触发自己声明 ----------

def test_every_trigger_declares_its_variables_and_matches_only_return_declared_ones() -> None:
    declared = {item.key: {v.name for v in variables_of(item)} for item in all_triggers()}
    assert {"sender_name", "patted_name", "group", "time"} == declared["pat"]
    assert {"member_name", "member_names", "member_count", "group", "time"} == declared["member_join"]
    assert {"new_name", "member_name"} <= declared["member_rename"] and "old_name" not in declared["member_rename"]
    assert {item.name for item in COMMON_VARIABLES} == {"group", "time"}
    for item in all_triggers():
        assert all(v.label and v.example for v in item.variables), item.key  # 每个变量都有中文说明和示例


def test_unknown_placeholders_are_found_per_trigger() -> None:
    pat, join = get_trigger("pat"), get_trigger("member_join")
    assert unknown_placeholders("{sender_name} 拍了 {patted_name},在 {group}", pat) == []
    assert unknown_placeholders("欢迎 {member_name} 和 {sender_name} {sender_name} {oops}", join) == ["sender_name", "oops"]
    assert unknown_placeholders("普通文字 {} { x } {1abc}", pat) == []  # 不像变量的花括号不管


def test_using_a_variable_the_trigger_does_not_provide_is_rejected_with_the_available_list() -> None:
    with pytest.raises(engine.RuleError, match=r"\{member_name\} 在「有人拍一拍」下不能用.*\{sender_name\}"):
        engine.validate_draft(_draft(actions=[{"type": "text", "content": "欢迎 {member_name}"}]), tenant_id=1)
    with pytest.raises(engine.RuleError, match="卡片标题"):
        engine.validate_draft(_draft(actions=[{"type": "card", "title": "{nope}", "url": "https://e.com"}]), tenant_id=1)
    engine.validate_draft(_draft(trigger_type="member_join", trigger_params={}, actions=[{"type": "text", "content": "欢迎 {member_name}"}]), tenant_id=1)


def _member_event(change_type: str, *, names=("小明",), message_type="group_member_join", **extra) -> dict:
    change = {"change_type": change_type, "members": [{"external_id": f"wxid_{i}", "display_name": name} for i, name in enumerate(names)], "after_count": 58, **extra}
    return {
        "conversation": {"chat_type": "group", "external_id": "g@chatroom"},
        "message": {"message_type": message_type, "content": "x", "sender_id": ""},
        "details": {"member_change": change},
    }


def test_member_triggers_provide_their_own_variables(env, sent) -> None:
    save_state(env.room, "trigger_reply", enabled=True)
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(name="欢迎", trigger_type="member_join", trigger_params={}, actions=[{"type": "text", "content": "欢迎 {member_names} 加入{group},现在 {member_count} 人"}]))
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(name="告别", trigger_type="member_leave", trigger_params={}, actions=[{"type": "text", "content": "{member_name} 走了"}]))
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(name="改名", trigger_type="member_rename", trigger_params={}, actions=[{"type": "text", "content": "{member_name} 改名叫 {new_name}"}]))

    def fire(event):
        sent.clear()
        executed = dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id)
        return executed, (sent[-1]["replies"][0]["content"] if sent else None)

    assert fire(_member_event("join", names=("小明", "小红"))) == (["trigger_reply"], "欢迎 小明、小红 加入测试群,现在 58 人")
    assert fire(_member_event("left", message_type="group_member_leave")) == (["trigger_reply"], "小明 走了")
    assert fire(_member_event("removed", message_type="group_member_leave"))[1] == "小明 走了"  # 被移出也算退群
    assert fire(_member_event("rename", message_type="group_member_nickname_change", new_room_nick_name="明明")) == (["trigger_reply"], "小明 改名叫 明明")
    # 入群事件不会触发"退群"规则,普通文字不会触发任何成员规则
    plain = {"conversation": {"chat_type": "group", "external_id": "g@chatroom"}, "message": {"message_type": "text", "content": "你好", "sender_id": "x"}}
    assert fire(plain) == ([], None)


def test_a_declared_variable_the_event_could_not_provide_becomes_empty_not_a_raw_placeholder(env) -> None:
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft(name="欢迎", trigger_type="member_join", trigger_params={}, actions=[{"type": "text", "content": "现在{member_count}人"}]))
    info = {"change_type": "join", "members": [{"external_id": "w", "display_name": "小明"}]}  # 微桥没给群人数
    view = EventView(message_type="group_member_join", text="", sender_id="", sender_name="", group_name="群", member_change=info)
    assert _run(env, view) == [{"type": "text", "content": "现在人"}]


# ---------- 接入群功能分发 ----------

@pytest.fixture()
def sent(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(dispatch_module, "send_replies", lambda db, channel, conversation, replies, **kw: calls.append({"replies": replies, **kw}))
    return calls


def _pat_event(patted: str = "wxid_bot", is_me: bool | None = True) -> dict:
    """按协议 4.3.3 整理后的拍一拍入站事件(content 只是展示用文案,触发器不看它)。"""

    pat = {"from_id": "wxid_red", "patted_id": patted}
    if is_me is not None:
        pat["patted_is_me"] = is_me
    return {
        "conversation": {"chat_type": "group", "external_id": "g@chatroom"},
        "message": {"message_type": "pat", "content": "文案随便写,不参与匹配", "sender_id": "wxid_red"},
        "details": {"pat": pat},
    }


def test_a_pat_event_flows_through_the_group_feature_and_respects_its_switch(env, sent) -> None:
    engine.create_rule(env.db, tenant_id=1, chatroom_id=env.room.id, draft=_draft())
    # 名字按 ID 查群成员表:拍的人在本群的群昵称是"小红"
    env.db.add(ChatroomMember(tenant_id=1, chatroom_id=env.room.id, bot_id=1, channel_id=env.channel.id, member_external_id="wxid_red", display_name="小红", status="active"))
    env.db.flush()
    event = _pat_event()
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == []  # 功能默认关闭
    save_state(env.room, "trigger_reply", enabled=True)
    assert dispatch_group_features(env.db, env.channel, event, conversation_id=env.conversation.id) == ["trigger_reply"]
    assert sent[-1]["replies"] == [{"type": "text", "content": "别拍啦,小红"}]
    # 不相关的消息:没有规则命中,不计入已执行
    assert dispatch_group_features(env.db, env.channel, _pat_event('wxid_other', False), conversation_id=env.conversation.id) == []
    plain = {"conversation": {"chat_type": "group", "external_id": "g@chatroom"}, "message": {"message_type": "text", "content": "你好", "sender_id": "x"}}
    assert dispatch_group_features(env.db, env.channel, plain, conversation_id=env.conversation.id) == []


def test_the_webridge_adapter_builds_pat_details_from_the_protocol_fields() -> None:
    from app.services.channel_adapters.wechat_webridge import _build_pat_info

    body = {"pat_from_user": "wxid_red", "pat_patted_user": "wxid_bot", "pat_template": "文案", "content": "文案"}
    assert _build_pat_info(body, "wxid_bot") == {"from_id": "wxid_red", "patted_id": "wxid_bot", "patted_is_me": True}
    assert _build_pat_info({**body, "pat_patted_user": "wxid_other"}, "wxid_bot")["patted_is_me"] is False
    assert "patted_is_me" not in _build_pat_info(body, "")                         # 本账号 ID 未知:不猜
    assert "patted_is_me" not in _build_pat_info({"pat_from_user": "wxid_red"}, "wxid_bot")  # 缺被拍的人:不猜
    # 文案和旧版猜测的字段名一律不读
    assert _build_pat_info({"pat_template": '"小红" 拍了拍 我', "patted_is_self": True, "pat_patted": "wxid_bot"}, "wxid_bot") == {"from_id": "", "patted_id": ""}


# ---------- 管理页面 ----------

@pytest.fixture()
def pages(provider_page_context, monkeypatch):  # noqa: F811
    client, ids = provider_page_context
    monkeypatch.setattr(group_triggers_web, "TenantSessionLocal", ids["factory"])  # 夹具只替换了它列出的路由模块
    with ids["factory"]() as db:
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=ids["channel_id"], external_id="p@chatroom", name="页面测试群", is_active=True)
        db.add(room)
        db.commit()
        base = f"/channels/{ids['channel_id']}/chatrooms/{room.id}/triggers"
        return client, ids, int(room.id), base


def _form(**extra) -> dict:
    data = {
        "name": "拍一拍回复", "trigger_type": "pat", "t__pat__scope": "me", "pick_mode": "all", "cooldown_seconds": "30", "is_enabled": "1",
        "action_idx": ["0"], "a0__type": "text", "a0__text__content": "你好 {sender_name}",
    }
    data.update(extra)
    return data


def test_the_management_pages_walk_through_create_edit_toggle_delete(pages) -> None:
    client, ids, room_id, base = pages
    csrf = _login(client, "admin")
    assert "还没有触发规则" in client.get(base).text and "本群还没有打开" in client.get(base).text
    form = client.get(base + "/new").text
    # 变量按触发分别展示成可点击的标签,带中文说明;允许用变量的输入框有标记
    assert 'data-tr-insert="{sender_name}"' in form and "拍的人" in form and 'data-tr-insert="{member_count}"' in form and "入群后的群人数" in form
    assert "data-tr-var-target" in form
    # 控件是产品自己的部件:触发条件是选择块,回复类型是分段选择,启用是开关,文件用自绘选择器;没有原生下拉和裸复选框
    assert 'class="tr-choice-grid"' in form and 'data-tr-action-type' in form and 'class="ui-switch ui-switch-small"' in form
    assert "<select" not in form and form.count('role="switch"') == 1 and 'name="is_enabled"' in form  # 没有原生下拉;勾选类的只有"保存后立即启用"这个开关
    assert "有人拍一拍" in form and "回复什么" in form and 'name="a0__type"' in form and 'id="tr-action-template"' in form

    created = client.post(base, data=_form(csrf_token=csrf), files={"a0__image__file": ("p.png", _png(), "image/png")})
    assert created.status_code == 303
    listing = client.get(base).text
    assert "拍一拍回复" in listing and "有人拍机器人时" in listing and "文字「你好 {sender_name}」" in listing and "冷却 30 秒" in listing
    with ids["factory"]() as db:
        (rule,) = db.scalars(select(GroupTriggerRule)).all()
        rule_id = int(rule.id)
        assert rule.cooldown_seconds == 30 and rule.chatroom_id == room_id

    # 加上一张图片再保存:换成图片类型,上传的文件被保存并在编辑页提示"已上传"
    saved = client.post(f"{base}/{rule_id}", data=_form(csrf_token=csrf, action_idx=["0", "1"], a1__type="image"), files={"a1__image__file": ("p.png", _png(), "image/png")})
    assert saved.status_code == 303
    edit = client.get(f"{base}/{rule_id}").text
    assert "已上传" in edit and "你好 {sender_name}" in edit
    with ids["factory"]() as db:
        stored = json.loads(db.scalar(select(GroupTriggerRule.actions_json)))[1]["file"]
    assert (media.media_root(ids["alpha"]) / stored).is_file()

    toggled = client.post(f"{base}/{rule_id}/toggle", json={"value": False}, headers={"X-CSRF-Token": csrf})
    assert toggled.json()["ok"] is True and "已停用" in toggled.json()["notice"]
    with ids["factory"]() as db:
        assert db.scalar(select(GroupTriggerRule.is_enabled)) is False

    assert client.post(f"{base}/{rule_id}/delete", data={"csrf_token": csrf}).status_code == 303
    assert not (media.media_root(ids["alpha"]) / stored).exists()  # 素材跟着删了
    assert "还没有触发规则" in client.get(base).text


def test_a_bad_submission_shows_the_message_and_keeps_what_was_typed(pages) -> None:
    client, ids, _room_id, base = pages
    csrf = _login(client, "admin")
    response = client.post(base, data=_form(csrf_token=csrf, name="我的规则", a0__text__content=""))
    assert response.status_code == 422 and "请填写「回复内容」" in response.text and 'value="我的规则"' in response.text
    bad_file = client.post(base, data=_form(csrf_token=csrf, action_idx=["0"], a0__type="image"), files={"a0__image__file": ("x.png", b"not an image", "image/png")})
    assert bad_file.status_code == 422 and "不是有效的图片" in bad_file.text
    with ids["factory"]() as db:
        assert db.scalars(select(GroupTriggerRule)).all() == []
    assert not media.media_root(ids["alpha"]).exists() or list(media.media_root(ids["alpha"]).iterdir()) == []  # 失败的上传不留文件


def test_permissions_and_csrf(pages) -> None:
    client, _ids, _room_id, base = pages
    _login(client, "readonly")
    assert client.get(base).status_code == 200  # 只读成员可以看
    assert client.get(base + "/new").status_code == 403 and client.post(base, data=_form(csrf_token="x")).status_code == 403
    csrf = _login(client, "operator")
    assert client.post(base, data=_form(csrf_token="forged")).status_code == 403  # 令牌不对
    assert client.post(base, data=_form(csrf_token=csrf)).status_code == 303
    client.cookies.clear()
    assert client.get(base).status_code in (303, 401)
    assert client.get("/channels/999/chatrooms/999/triggers").status_code in (303, 401, 404)
