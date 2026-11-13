"""全局约定:自己发的消息不进内容检测;拍一拍要能认出"拍的是机器人"。

线上现象(用户在 BOT-测试群里实测):
1. 拍一拍规则不触发。入站事件其实收到了,文案是 `"水牛" 拍了拍 "水牛@rCodex"`——被拍的人显示的是机器人自己的昵称,
   而匹配逻辑只认第一人称"我",所以"拍机器人自己时"这条规则永远不命中;
2. 文本检测规则把自己发的消息当成检测内容(机器人的回复被协议回流、命中规则,会形成自问自答的循环)。
"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="self-sent-logs-"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Channel, Chatroom, ChatroomMember, Conversation, TaskJob
from app.models.plugin_business import GroupTriggerRule
from app.services import async_workers
from app.services.channel_events import ingest_inbound_event
from app.services.group_features import dispatch as dispatch_module
from app.services.group_features.dispatch import dispatch_group_features
from app.services.group_features.store import save_state
from app.services.group_features.trigger_reply import bot_names
from app.services.self_sent import is_self_sent
from app.services.task_queue import PLUGIN_DISPATCH_JOB
from app.services.trigger_rules import engine
from app.services.trigger_rules.catalog import EventView, get_trigger, parse_pat
from tests.test_webridge_member_directory import IDENTITY, ROOM, env  # noqa: F401  (含 fixture)


# ---------- 统一判定 ----------

@pytest.mark.parametrize(
    "normalized, expected",
    [
        ({"details": {"from_self": True}}, True),                                   # 微桥、咕嘎助手:按注册的账号比对
        ({"message": {"direction": "out"}}, True),                                  # iPad、企业微信:方向写成 out
        ({"details": {"direction": "out"}}, True),
        ({"details": {"from_self": False}, "message": {"direction": "in"}}, False),
        ({"details": {}}, False),
        ({}, False),
        (None, False),
        ({"details": {"from_self": "yes"}}, False),                                 # 只认明确的 True,不把任意值当"是"
    ],
)
def test_the_shared_self_sent_rule(normalized, expected) -> None:
    assert is_self_sent(normalized) is expected


# ---------- 入站总闸:存档,但不进入任何检测 ----------

# 2030-03-18 11:46(北京时间):避开系统的免打扰时段 00:30-05:00,那个时段所有消息都只存档、不分发
DAYTIME = 1_900_036_000

def _say(env, sender: str, key: str, content: str = "你好"):
    db, channel, room, _ = env
    return ingest_inbound_event(db, channel, {"event_id": key, "event_name": "message", "body": {
        "room_id": room.external_id, "sender_id": sender, "sender_name": sender, "content": content, "create_time": DAYTIME,
    }})


def _dispatch_jobs(db) -> list[TaskJob]:
    return list(db.scalars(select(TaskJob).where(TaskJob.job_type == PLUGIN_DISPATCH_JOB)).all())


def test_a_message_from_the_bot_account_is_stored_but_never_dispatched(env) -> None:
    db, _channel, _room, _member = env
    mine = _say(env, IDENTITY["account_id"], "mine")           # 机器人账号自己发的(含回复回流)
    assert mine["dispatch"]["status"] == "skipped" and mine["dispatch"]["reason"] == "self_sent" and mine["dispatch"]["queued"] is False
    assert mine["mapped"] is True                                # 仍然入库留痕,消息中心看得到
    assert _dispatch_jobs(db) == []                              # 插件、群功能、触发规则、AI 全都收不到
    other = _say(env, "alice", "other")                          # 别人发的照常
    assert other["dispatch"]["queued"] is True and len(_dispatch_jobs(db)) == 1


def test_the_worker_also_refuses_self_sent_jobs_already_in_the_queue(env, monkeypatch) -> None:
    """部署时队列里可能已经排着自发消息的任务,处理任务这一层必须再拦一次。"""

    db, channel, room, _ = env
    called: list[str] = []
    monkeypatch.setattr(async_workers, "dispatch_group_features", lambda *a, **k: called.append("features") or [])
    monkeypatch.setattr(async_workers, "build_dispatch_report", lambda *a, **k: called.append("plugins"))
    normalized = {"event_type": "message", "details": {"from_self": True},
                  "message": {"content": "你好", "sender_id": "a1", "message_type": "text"}, "conversation": {"external_id": room.external_id, "chat_type": "group"}}
    result = async_workers._process_plugin_dispatch_job(db, {"channel_id": channel.id, "normalized_event": normalized, "raw_event_id": 1, "conversation_id": 1, "message_id": 1})
    assert called == [] and result["executed_plugins"] == [] and result["executed_group_features"] == [] and result["dispatch"]["reason"] == "self_sent"


# ---------- 触发规则:机器人的回复回流不会再命中关键词规则 ----------

@pytest.fixture()
def rule_env():
    engine_ = create_engine("sqlite://")
    for table in (Channel, Conversation, Chatroom, ChatroomMember, GroupTriggerRule):
        table.__table__.create(engine_)
    factory = sessionmaker(bind=engine_, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            channel = Channel(tenant_id=1, bot_id=1, name="微桥", channel_type="wechat_webridge", status="online", is_enabled=True,
                              runtime_info_json=json.dumps({"webridge": {"account_id": "wxid_bot"}, "account_profile": {"nickname": "水牛@rCodex"}}))
            db.add(channel)
            db.flush()
            conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group")
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="BOT-测试", is_active=True)
            db.add_all([conversation, room])
            db.flush()
            save_state(room, "trigger_reply", enabled=True)
            yield db, channel, conversation, room


@pytest.fixture()
def sent(monkeypatch):
    calls: list = []
    monkeypatch.setattr(dispatch_module, "send_replies", lambda db, channel, conversation, replies, **kw: calls.append(replies))
    return calls


def _draft(trigger_type: str, params: dict, text: str):
    return engine.RuleDraft(name="规则", trigger_type=trigger_type, trigger_params=params, actions=[{"type": "text", "content": text}])


def _pat_event(text: str, message_type: str = "pat") -> dict:
    return {"conversation": {"chat_type": "group", "external_id": "g@chatroom"},
            "message": {"message_type": message_type, "content": text, "sender_id": "wxid_main", "sender_name": "水牛"}, "details": {}}


# ---------- 拍一拍 ----------

def test_the_bots_own_names_are_collected_from_every_place_the_platform_keeps_them(rule_env) -> None:
    db, channel, _conversation, room = rule_env
    db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="wxid_bot", display_name="机器人群昵称", nickname="机器人微信昵称", status="active"))
    db.flush()
    names = bot_names(db, channel, room)
    assert {"水牛@rCodex", "机器人群昵称", "机器人微信昵称", "wxid_bot"} <= set(names)


@pytest.mark.parametrize("message_type", ["pat", "system"])
def test_patting_the_bot_triggers_the_rule_with_the_real_world_text(rule_env, sent, message_type) -> None:
    """线上实测的文案:被拍的人显示为机器人的昵称。pat 事件和 system 消息两种到达形态都要认。"""

    db, channel, conversation, room = rule_env
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "me"}, "别拍啦,{sender_name}"))
    executed = dispatch_group_features(db, channel, _pat_event('"水牛" 拍了拍 "水牛@rCodex"', message_type), conversation_id=conversation.id)
    assert executed == ["trigger_reply"] and sent == [[{"type": "text", "content": "别拍啦,水牛"}]]


def test_patting_someone_else_does_not_trigger_a_bot_only_rule(rule_env, sent) -> None:
    db, channel, conversation, room = rule_env
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "me"}, "别拍啦"))
    assert dispatch_group_features(db, channel, _pat_event('"水牛" 拍了拍 "小牛"'), conversation_id=conversation.id) == [] and sent == []
    # "拍任何人"的规则则会回应
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "any"}, "拍得好"))
    assert dispatch_group_features(db, channel, _pat_event('"水牛" 拍了拍 "小牛"'), conversation_id=conversation.id) == ["trigger_reply"]


def test_a_pat_with_a_suffix_is_split_so_the_name_is_clean() -> None:
    """线上另一种形态:`"水牛" 拍了拍 "学习使我快乐" 的棺材板说:挤一下`。后缀不能混进被拍的人的名字里。"""

    view = EventView(message_type="pat", text='"水牛" 拍了拍 "学习使我快乐" 的棺材板说：挤一下', sender_id="x", sender_name="n", group_name="g", self_names=("水牛@rCodex",))
    info = parse_pat(view)
    assert (info.from_name, info.patted_name, info.patted_is_me) == ("水牛", "学习使我快乐", False)
    # 一个叫"我是谁"的人被拍,不能被当成"拍的是我"(第一人称只出现在不带引号的文案里)
    assert parse_pat(EventView(message_type="pat", text='"水牛" 拍了拍 "我是谁"', sender_id="x", sender_name="n", group_name="g", self_names=("水牛@rCodex",))).patted_is_me is False
    assert parse_pat(EventView(message_type="pat", text='"水牛" 拍了拍 我', sender_id="x", sender_name="n", group_name="g")).patted_is_me is True  # 旧写法仍认


def test_names_are_compared_loosely_but_not_by_substring() -> None:
    pat = get_trigger("pat")
    view = lambda target: EventView(message_type="pat", text=f'"水牛" 拍了拍 "{target}"', sender_id="x", sender_name="n", group_name="g", self_names=("水牛@rCodex",))
    assert pat.match({"scope": "me"}, view("水牛@rCodex")) is not None
    assert pat.match({"scope": "me"}, view(" 水牛@RCODEX ")) is not None   # 大小写、首尾空白不影响
    assert pat.match({"scope": "me"}, view("水牛")) is None                 # 只是名字的一部分:不是机器人(用户本人也叫"水牛")


def test_when_the_bots_names_are_unknown_it_stays_quiet_instead_of_guessing() -> None:
    pat = get_trigger("pat")
    view = EventView(message_type="pat", text='"水牛" 拍了拍 "水牛@rCodex"', sender_id="x", sender_name="n", group_name="g")  # 没有任何称呼可比对
    # 没有任何称呼可比对时,被拍的是不是机器人无从判断:"只回应拍机器人"的规则不触发,宁可不回也不乱回
    assert pat.match({"scope": "me"}, view) is None
    assert pat.match({"scope": "any"}, view) is not None  # "拍任何人"的规则不依赖这个判断,照常触发


def test_plain_text_that_merely_mentions_the_phrase_is_not_a_pat(rule_env, sent) -> None:
    db, channel, conversation, room = rule_env
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "any"}, "拍得好"))
    assert dispatch_group_features(db, channel, _pat_event('我觉得 "水牛" 拍了拍 "小牛" 很可爱', "text"), conversation_id=conversation.id) == []
