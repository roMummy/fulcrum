"""群聊记忆批量整理:阈值(30 条或空闲 20 分钟)、过滤、游标、上限与完整编排。"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import timedelta

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="ai-digest-logs-"))

import pytest

from app.core.settings import settings
from app.core.time import now_shanghai
from app.models.entities import Chatroom, Conversation, Message, TenantAiSetting
from app.services import ai_dispatch, ai_memory, ai_memory_digest as digest
from app.services.group_features.store import save_state
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from tests.test_ai_memory import _FakeHindsight, hindsight  # noqa: F401  (hindsight 是 fixture)

NOW = now_shanghai()


@pytest.fixture()
def env():
    db = _session()
    channel = _seed(db)
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="1@chatroom", name="养生群", is_active=True)
    conversation = Conversation(id=2, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="group")
    db.add_all([room, conversation])
    db.query(TenantAiSetting).update({"chat_enabled": True})
    db.commit()
    save_state(room, "ai_chat", enabled=True, config={"memory_all": True})
    db.commit()
    return db, room, conversation


def _add(db, texts, *, name="小明", minutes_ago=1.0, direction="in", message_type="text") -> list[Message]:
    rows = []
    for i, text in enumerate(texts):
        row = Message(
            conversation_id=2, direction=direction, sender_id="wxid_a", sender_name=name, content=text, message_type=message_type,
            created_at=NOW - timedelta(minutes=minutes_ago, seconds=len(texts) - i),
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def _texts(n: int, start: int = 0) -> list[str]:
    return [f"今天天气真不错啊第{i}条" for i in range(start, start + n)]


def _start(env):
    """首次扫描:把游标定位到当前最新消息,之后新增的消息才会被整理。"""

    db, room, conversation = env
    assert digest.plan_group_digest(db, room, conversation, now=NOW) is None
    db.commit()


# ---------- 有效消息 ----------

def test_eligibility_filters_commands_cards_and_chatter() -> None:
    assert digest.is_eligible("我下个月要去日本旅游")
    assert not digest.is_eligible("好的好的")  # 过短
    assert not digest.is_eligible("/签到 今天")  # 命令
    assert not digest.is_eligible("#帮助 一下吧")
    assert not digest.is_eligible('wxid_x:\n<?xml version="1.0"?><msg>卡片</msg>')
    assert not digest.is_eligible("很长的文本" * 3, message_type="image")
    assert digest.clean_text("wxid_abc123:\n  我   住在上海\n很好  ") == "我 住在上海 很好"


# ---------- 游标与阈值 ----------

def test_first_scan_starts_from_now_without_backfilling_history(env) -> None:
    db, room, conversation = env
    _add(db, _texts(40), minutes_ago=300)  # 开启之前的历史
    assert digest.plan_group_digest(db, room, conversation, now=NOW) is None
    assert digest._cursor(room)["message_id"] == max(m.id for m in db.query(Message).all())
    assert digest.plan_group_digest(db, room, conversation, now=NOW) is None  # 历史不会被整理


def test_waits_below_threshold_then_flushes_at_thirty(env) -> None:
    db, room, conversation = env
    _start(env)
    _add(db, _texts(29), minutes_ago=1)
    assert digest.plan_group_digest(db, room, conversation, now=NOW) is None  # 29 条,刚发的:继续攒
    _add(db, ["第三十条也是有效消息"], minutes_ago=0.5)
    plan = digest.plan_group_digest(db, room, conversation, now=NOW)
    assert plan is not None and plan.count == 30
    lines = plan.content.split("\n")
    label = ai_memory.speaker_label("wxid_a")
    assert len(lines) == 30 and lines[0] == f"{label}: 今天天气真不错啊第0条"
    assert "小明" not in plan.content  # 记忆里不出现昵称
    assert plan.bank_id == "t1-1@chatroom" and plan.bank_name == "养生群"


def test_flushes_after_twenty_idle_minutes_but_not_before(env) -> None:
    db, room, conversation = env
    _start(env)
    _add(db, _texts(5), minutes_ago=19)
    assert digest.plan_group_digest(db, room, conversation, now=NOW) is None  # 才空闲 19 分钟
    later = NOW + timedelta(minutes=2)  # 再过 2 分钟:最后一条已空闲 21 分钟
    plan = digest.plan_group_digest(db, room, conversation, now=later)
    assert plan is not None and plan.count == 5


def test_transcript_uses_wxids_never_nicknames_and_filters_noise(env) -> None:
    db, room, conversation = env
    _start(env)
    _add(db, ["我最近搬到上海了，换了新工作"], name="北京小王", minutes_ago=30)  # 带误导性的昵称
    _add(db, ["我家的猫叫团子，特别能吃"], name="wxid_abcdef123", minutes_ago=29)
    rows = _add(db, ["我买了紫铜版的散热件"], name="我二弟最牛逼！", minutes_ago=28)
    rows[0].sender_id = "wxid_c"
    _add(db, ["/签到 今天来了呀", "好的", '<?xml version="1.0"?><msg>x</msg>'], name="小红", minutes_ago=27)
    _add(db, ["机器人自己的回复内容也很长呢"], direction="out", minutes_ago=26)  # 出站,不在其中
    _add(db, ["没有发言人ID的消息也不能记"], minutes_ago=25)[0].sender_id = None
    db.commit()
    plan = digest.plan_group_digest(db, room, conversation, now=NOW)
    a_, c_ = "wxid_a", "wxid_c"  # 记忆里是发言人的微信 ID,事后可对账
    assert plan.content.split("\n") == [
        f"{a_}: 我最近搬到上海了，换了新工作",
        f"{a_}: 我家的猫叫团子，特别能吃",  # 同一个人(同一 sender_id)始终是同一个 ID
        f"{c_}: 我买了紫铜版的散热件",
    ]
    # 整份记录里不出现任何昵称(包括带误导性的"北京小王"、"我二弟最牛逼",以及昵称恰好是另一个 wxid 的情况)
    for nickname in ("北京小王", "我二弟最牛逼", "小红", "wxid_abcdef123"):
        assert nickname not in plan.content


def test_only_ineligible_messages_advance_the_cursor_without_flushing(env) -> None:
    db, room, conversation = env
    _start(env)
    rows = _add(db, ["好的", "/帮助 我要帮助", "哈哈"], minutes_ago=60)
    assert digest.plan_group_digest(db, room, conversation, now=NOW) is None
    assert digest._cursor(room)["message_id"] == rows[-1].id


# ---------- 上限 ----------

def test_batch_is_capped_and_the_rest_waits_for_the_next_batch(env) -> None:
    db, room, conversation = env
    _start(env)
    rows = _add(db, _texts(75), minutes_ago=1)
    plan = digest.plan_group_digest(db, room, conversation, now=NOW)
    assert plan.count == digest.MAX_BATCH and plan.cursor_message_id == rows[digest.MAX_BATCH - 1].id
    digest.commit_flush(room, plan, now=NOW)
    nxt = digest.plan_group_digest(db, room, conversation, now=NOW)  # 剩 15 条,刚发的:继续攒
    assert nxt is None
    later = digest.plan_group_digest(db, room, conversation, now=NOW + timedelta(minutes=25))
    assert later is not None and later.count == 15


def test_character_budget_limits_a_batch(env) -> None:
    db, room, conversation = env
    _start(env)
    _add(db, ["长" * 300] * 40, minutes_ago=1)
    plan = digest.plan_group_digest(db, room, conversation, now=NOW)
    assert len(plan.content) <= digest.MAX_BATCH_CHARS + 400 and plan.count < 40


def test_daily_flush_cap(env) -> None:
    db, room, conversation = env
    _start(env)
    _add(db, _texts(30), minutes_ago=1)
    plan = digest.plan_group_digest(db, room, conversation, now=NOW)
    digest._save_cursor(room, {"message_id": digest._cursor(room)["message_id"], "day": NOW.date().isoformat(), "flushes": digest.MAX_FLUSHES_PER_DAY})
    assert digest.plan_group_digest(db, room, conversation, now=NOW) is None
    digest.commit_flush(room, plan, now=NOW + timedelta(days=1))  # 第二天计数重置
    assert digest._cursor(room)["flushes"] == 1


# ---------- 完整编排 ----------

@pytest.fixture()
def wired(env, hindsight, monkeypatch):  # noqa: F811
    """把 run_for_tenant 里的租户会话换成测试库,记忆服务换成假服务。"""

    db, room, conversation = env

    @contextmanager
    def fake_session(_factory):
        yield db

    monkeypatch.setattr("app.core.tenant_db.tenant_session", fake_session)
    return db, room, conversation, hindsight


def test_run_flushes_one_untagged_async_transcript_and_advances_the_cursor(wired) -> None:
    db, room, conversation, hs = wired
    _start((db, room, conversation))
    _add(db, ["我下个月要去日本旅游，想吃拉面"] + _texts(29), minutes_ago=1)
    assert digest.run_for_tenant(now=NOW) == 1
    _, path, body = hs.requests[-1]
    item = body["items"][0]
    label = ai_memory.speaker_label("wxid_a")
    assert path == "/v1/default/banks/t1-1@chatroom/memories" and body["async"] is True
    assert "tags" not in item and "多人对话" in item["context"] and item["content"].startswith(f"{label}: 我下个月要去日本旅游")
    # 库有可读的显示名(群名)和抽取规则;发给记忆服务的所有内容里没有昵称
    put = next(r for r in hs.requests if r[0] == "PUT")
    assert put[1] == "/v1/default/banks/t1-1@chatroom" and put[2]["name"] == "养生群" and "昵称" in put[2]["retain_mission"]
    assert "小明" not in json.dumps([r[2] for r in hs.requests], ensure_ascii=False)
    assert digest._cursor(room)["message_id"] == max(m.id for m in db.query(Message).all()) and digest._cursor(room)["flushes"] == 1
    # 已整理的不会再整理
    assert digest.run_for_tenant(now=NOW) == 0


def test_run_keeps_the_cursor_when_the_memory_service_fails(wired, monkeypatch) -> None:
    db, room, conversation, hs = wired
    _start((db, room, conversation))
    rows = _add(db, _texts(30), minutes_ago=1)
    before = digest._cursor(room)["message_id"]
    working_url = settings.hindsight_url
    monkeypatch.setattr(settings, "hindsight_url", "http://127.0.0.1:1")  # 连不上
    monkeypatch.setattr(ai_memory, "_down_until", 0.0)
    assert digest.run_for_tenant(now=NOW) == 0
    assert digest._cursor(room)["message_id"] == before  # 没推进,恢复后会重试
    monkeypatch.setattr(settings, "hindsight_url", working_url)
    monkeypatch.setattr(ai_memory, "_down_until", 0.0)
    assert digest.run_for_tenant(now=NOW) == 1 and digest._cursor(room)["message_id"] == rows[-1].id


def test_run_skips_groups_without_the_switch_or_inactive_ones(wired) -> None:
    db, room, conversation, hs = wired
    _start((db, room, conversation))
    _add(db, _texts(30), minutes_ago=1)
    save_state(room, "ai_chat", enabled=True, config={"memory_all": False})
    assert digest.run_for_tenant(now=NOW) == 0  # 没开"记住所有聊天"
    save_state(room, "ai_chat", enabled=True, config={"memory_all": True})
    room.is_active = False
    assert digest.run_for_tenant(now=NOW) == 0  # 群未启用
    room.is_active = True
    save_state(room, "ai_chat", enabled=True, config={"memory_all": True, "memory_enabled": False})
    assert digest.run_for_tenant(now=NOW) == 0  # 长期记忆总开关关了
    assert hs.requests == []


def test_run_is_a_noop_without_a_memory_service(env, monkeypatch) -> None:
    monkeypatch.setattr(settings, "hindsight_url", "")
    assert digest.run_for_tenant(now=NOW) == 0


# ---------- 与聊天时的单独写入互斥 ----------

def test_memory_all_flag_reaches_the_chat_context_so_the_immediate_retain_is_skipped(env, monkeypatch) -> None:
    db, room, conversation = env
    monkeypatch.setattr(settings, "hindsight_url", "http://hindsight:8888")
    payload = {
        "channel_id": 1,
        "conversation_id": 2,
        "normalized_event": {
            "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
            "message": {"content": "你好呀", "sender_id": "wxid_a", "sender_name": "小明", "message_type": "text"},
            "details": {"mentioned_self": True},
        },
    }
    assert ai_dispatch.prepare_intent(db, payload)[3].memory_all is True
    save_state(room, "ai_chat", enabled=True, config={"memory_all": False})
    assert ai_dispatch.prepare_intent(db, payload)[3].memory_all is False
