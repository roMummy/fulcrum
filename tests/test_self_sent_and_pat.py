"""全局约定:自己发的消息不进内容检测;拍一拍要能认出"拍的是机器人"。

线上现象(用户在 测试群里实测):
1. 拍一拍规则不触发。入站事件其实收到了,文案是 `"小明" 拍了拍 "小机@bot"`——被拍的人显示的是机器人自己的昵称,
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
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.self_sent import is_self_sent
from app.services.task_queue import PLUGIN_DISPATCH_JOB
from app.services.trigger_rules import engine
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
                              runtime_info_json=json.dumps({"webridge": {"account_id": "wxid_bot"}, "account_profile": {"nickname": "小机@bot"}}))
            db.add(channel)
            db.flush()
            conversation = Conversation(tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="g@chatroom", chat_type="group")
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="g@chatroom", name="测试群", is_active=True)
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


def _pat_payload(*, patted: str, sender: str = "wxid_main", content: str = "文案随便写,不参与匹配", msg_type: str = "pat", envelope_account: str = "") -> dict:
    """协议文档 4.3.3 的拍一拍入站事件:event_name=message、msg_type=pat,拍的人/被拍的人是 wxid。"""

    body = {"message_id": "m-pat", "chat_id": "g@chatroom", "room_id": "g@chatroom", "is_group": True, "sender_id": sender,
            "msg_type": msg_type, "content": content, "create_time": DAYTIME, "pat_from_user": sender, "pat_patted_user": patted, "pat_template": content}
    return {"event_id": "msg:m-pat", "event_name": "message", **({"account_id": envelope_account} if envelope_account else {}), "body": body}


def _normalize(channel, payload: dict) -> dict:
    return WechatWeBridgeAdapter().normalize_inbound_payload(channel, payload)


def _know_main(db, channel, room) -> None:
    db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=channel.id, member_external_id="wxid_main", display_name="小明", status="active"))
    db.flush()


# ---------- 拍一拍:按协议的结构化字段判断,不看文案 ----------

def test_patting_the_bot_triggers_the_rule_using_only_the_ids(rule_env, sent) -> None:
    db, channel, conversation, room = rule_env
    _know_main(db, channel, room)
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "me"}, "别拍啦,{sender_name}"))
    # 文案故意写成和"拍了拍"毫不相干的内容:能触发就证明用的是 ID,不是文本匹配
    event = _normalize(channel, _pat_payload(patted="wxid_bot", content="完全不同的一段文字"))
    assert event["details"]["pat"] == {"from_id": "wxid_main", "patted_id": "wxid_bot", "patted_is_me": True}
    assert dispatch_group_features(db, channel, event, conversation_id=conversation.id) == ["trigger_reply"]
    assert sent == [[{"type": "text", "content": "别拍啦,小明"}]]  # 名字按 ID 查群成员表得到


def test_patting_someone_else_does_not_trigger_a_bot_only_rule(rule_env, sent) -> None:
    db, channel, conversation, room = rule_env
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "me"}, "别拍啦"))
    event = _normalize(channel, _pat_payload(patted="wxid_someone"))
    assert event["details"]["pat"]["patted_is_me"] is False
    assert dispatch_group_features(db, channel, event, conversation_id=conversation.id) == [] and sent == []
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "any"}, "拍得好"))  # "拍任何人"的规则则会回应
    assert dispatch_group_features(db, channel, event, conversation_id=conversation.id) == ["trigger_reply"]


def test_the_account_id_falls_back_to_the_event_envelope_when_the_phone_never_registered(rule_env, sent) -> None:
    """协议事件信封里本来就带 account_id;手机注册快照缺失时用它,同样能认出被拍的是机器人。"""

    db, channel, conversation, room = rule_env
    channel.runtime_info_json = json.dumps({})  # 没有登记的账号
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "me"}, "别拍啦"))
    assert dispatch_group_features(db, channel, _normalize(channel, _pat_payload(patted="wxid_bot", envelope_account="wxid_bot")), conversation_id=conversation.id) == ["trigger_reply"]


def test_when_the_bots_own_id_is_unknown_the_bot_only_rule_stays_quiet(rule_env, sent) -> None:
    db, channel, conversation, room = rule_env
    channel.runtime_info_json = json.dumps({})
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "me"}, "别拍啦"))
    event = _normalize(channel, _pat_payload(patted="wxid_bot"))  # 没有登记账号,信封里也没有 account_id
    assert "patted_is_me" not in event["details"]["pat"]
    assert dispatch_group_features(db, channel, event, conversation_id=conversation.id) == [] and sent == []  # 判断不了就不回,不乱回


def test_the_bot_patting_someone_is_a_self_sent_event_and_never_dispatched(rule_env) -> None:
    _db, channel, _conversation, _room = rule_env
    event = _normalize(channel, _pat_payload(patted="wxid_main", sender="wxid_bot"))
    assert event["details"]["from_self"] is True and is_self_sent(event) is True  # 机器人自己拍别人:不会再触发规则、形成循环


def test_text_that_looks_like_a_pat_is_not_a_pat(rule_env, sent) -> None:
    """只有协议标注 msg_type=pat 的事件才是拍一拍;有人发一句长得像拍一拍的话,不能触发。"""

    db, channel, conversation, room = rule_env
    engine.create_rule(db, tenant_id=1, chatroom_id=room.id, draft=_draft("pat", {"scope": "any"}, "拍得好"))
    event = _normalize(channel, _pat_payload(patted="wxid_bot", msg_type="text", content='"小明" 拍了拍 "小机@bot"'))
    assert "pat" not in event["details"]
    assert dispatch_group_features(db, channel, event, conversation_id=conversation.id) == [] and sent == []
