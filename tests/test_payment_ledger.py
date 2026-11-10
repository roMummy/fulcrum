"""收款记录:红包/转账入库(来源、金额、结果)、重复事件、统计与明细,以及两处页面里的折叠卡片。"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import timedelta
from unittest.mock import patch

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="payment-ledger-logs-"))

import pytest
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.entities import Chatroom, Conversation, PaymentReceipt, TaskJob
from app.services import payment_ledger as ledger
from app.services.group_features import red_packet
from app.services.group_features.dispatch import dispatch_group_features
from app.services.group_features.store import save_state
from app.services.wechat_webridge import auto_receive, service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from tests.test_auto_receive_transfer import IDENTITY, _event as transfer_event, webridge_channel  # noqa: F401  (webridge_channel 是 fixture)
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

PACKET = "rp_msg_100"


@pytest.fixture()
def env(monkeypatch):
    from contextlib import contextmanager

    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    auto_receive.set_enabled(channel, True)
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="1@chatroom", name="养生群", is_active=True)
    db.add_all([
        room,
        Conversation(id=3, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="group", title="养生群"),
        Conversation(id=4, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="wxid_alice", chat_type="direct", title="张三"),
    ])
    db.commit()
    save_state(room, "red_packet", enabled=True, config={"delay_ms": 0})
    db.commit()
    monkeypatch.setattr(red_packet, "_schedule", lambda delay, func, *args: None)
    return db, channel, room


def _packet_event(packet: str = PACKET, sender: str = "wxid_boss", name: str = "老板") -> dict:
    return {
        "conversation": {"external_id": "1@chatroom", "chat_type": "group"},
        "message": {"content": "red_packet_received", "sender_id": sender, "sender_name": name, "message_type": "red_packet"},
        "details": {"payment": {"red_packet_id": packet}},
    }


def _receipts(db) -> list[PaymentReceipt]:
    return list(db.scalars(select(PaymentReceipt).order_by(PaymentReceipt.id)).all())


# ---------- 金额提取 ----------

@pytest.mark.parametrize(
    "source, expected",
    [
        ({"amount_fen": 88}, 88),
        ({"amount_fen": "88"}, 88),
        ({"amount_value": "0.88"}, 88),
        ({"amount_text": "￥1,234.50"}, 123450),
        ({"amount_yuan": 0.02}, 2),
        ({"data": {"amount_fen": 5}}, 5),  # 一层嵌套
        ({"amount_fen": True}, None),  # 布尔不是金额
        ({"amount_text": "来晚了"}, None),
        ({}, None),
    ],
)
def test_amount_extraction_handles_the_common_shapes(source, expected) -> None:
    assert ledger.extract_amount_fen(source) == expected


def test_money_is_formatted_in_yuan() -> None:
    assert ledger.format_yuan(88) == "¥0.88" and ledger.format_yuan(123450) == "¥1,234.50" and ledger.format_yuan(None) == "-"


# ---------- 入库与去重 ----------

def test_red_packet_event_is_recorded_with_its_source_and_group_before_the_claim(env) -> None:
    db, channel, _room = env
    assert dispatch_group_features(db, channel, _packet_event(), conversation_id=3) == ["red_packet"]
    row = _receipts(db)[0]
    assert (row.kind, row.external_id, row.status, row.amount_fen) == ("red_packet", PACKET, "pending", None)
    # 来源:发红包的人(微信 ID + 当时的昵称)和群(外部 ID + 群名)
    assert (row.sender_id, row.sender_name, row.chat_external_id, row.chat_title, row.chat_type) == ("wxid_boss", "老板", "1@chatroom", "养生群", "group")


def test_a_repeated_event_does_not_create_a_second_record_or_a_second_claim(env, monkeypatch) -> None:
    db, channel, _room = env
    scheduled = []
    monkeypatch.setattr(red_packet, "_schedule", lambda delay, func, *args: scheduled.append(args))
    dispatch_group_features(db, channel, _packet_event(), conversation_id=3)
    dispatch_group_features(db, channel, _packet_event(), conversation_id=3)
    assert len(_receipts(db)) == 1 and len(scheduled) == 1


def test_own_red_packets_are_not_recorded(env) -> None:
    db, channel, _room = env
    dispatch_group_features(db, channel, _packet_event(sender="wxid_bot"), conversation_id=3)
    assert _receipts(db) == []


def test_transfer_event_is_recorded_with_amount_payer_and_chat(env) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify"):
        auto_receive.handle_event(db, channel, transfer_event(sender="wxid_alice", amount_fen=2), conversation_id=4)
        auto_receive.handle_event(db, channel, transfer_event(sender="wxid_alice", amount_fen=2), conversation_id=4)  # 重复投递
    row = _receipts(db)[0]
    assert len(_receipts(db)) == 1
    assert (row.kind, row.external_id, row.status, row.amount_fen) == ("transfer", "tx-1", "pending", 2)  # 事件里的 amount_text 为 ￥0.02
    assert (row.sender_id, row.chat_type, row.chat_title) == ("wxid_alice", "direct", "张三")


# ---------- 结果回填 ----------

def _run(db, channel, request_id: str, reported: dict) -> None:
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all():
        job.status = "running"
    db.commit()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service.accept_command_result(db, channel, {**IDENTITY, "request_id": request_id, **reported})


def test_transfer_confirmation_settles_the_record(env) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify"):
        auto_receive.handle_event(db, channel, transfer_event(sender="wxid_alice"), conversation_id=4)
    _run(db, channel, "transfer_tx-1", {"ok": True, "result": {"ok": True, "status": "callback_ok", "confirmed": True}})
    row = _receipts(db)[0]
    assert (row.status, row.result_status, row.amount_fen) == ("received", "callback_ok", 2) and row.settled_at is not None
    assert "callback_ok" in row.result_json  # 手机的原始回报留存,便于核对


@pytest.mark.parametrize(
    "reported, status",
    [
        ({"ok": True, "result": {"ok": True, "status": "confirm_invoked", "accepted": True, "confirmed": False}}, "unconfirmed"),
        ({"ok": False, "result": {"ok": False, "status": "callback_unknown"}}, "unconfirmed"),
        ({"ok": False, "result": {"ok": False, "status": "failed", "error_code": "NOT_RECEIVER"}}, "failed"),
    ],
)
def test_unconfirmed_and_failed_transfers_are_not_counted_as_received(env, reported, status) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify"):
        auto_receive.handle_event(db, channel, transfer_event(sender="wxid_alice"), conversation_id=4)
    _run(db, channel, "transfer_tx-1", reported)
    assert _receipts(db)[0].status == status


def test_red_packet_results_fill_status_and_amount(env) -> None:
    db, channel, _room = env
    for packet in ("rp_ok", "rp_late", "rp_broken"):
        dispatch_group_features(db, channel, _packet_event(packet=packet), conversation_id=3)
    # 把领取命令入队(定时器被测试替换掉了,这里直接走入队函数)
    with patch.object(wakeup, "notify"), patch.object(red_packet, "tenant_session", _same_session(db)):
        for packet in ("rp_ok", "rp_late", "rp_broken"):
            red_packet.claim(1, channel.id, 3, packet)
    _run(db, channel, "redpacket_rp_ok", {"ok": True, "result": {"ok": True, "status": "received", "amount_text": "￥0.88"}})
    _run(db, channel, "redpacket_rp_late", {"ok": False, "result": {"ok": False, "status": "fully_claimed"}})
    _run(db, channel, "redpacket_rp_broken", {"ok": False, "result": {"ok": False, "status": "not_logged_in"}})
    by_id = {row.external_id: row for row in _receipts(db)}
    assert (by_id["rp_ok"].status, by_id["rp_ok"].amount_fen) == ("received", 88)
    assert (by_id["rp_late"].status, by_id["rp_late"].amount_fen) == ("missed", None)  # 来晚了:被领完
    assert by_id["rp_broken"].status == "failed"


def _same_session(db):
    from contextlib import contextmanager

    @contextmanager
    def fake(_factory):
        yield db

    return fake


def test_a_result_without_a_record_is_harmless(env) -> None:
    """功能上线前入队的命令没有对应记录,回报时不报错。"""

    db, channel, _room = env
    assert ledger.settle(db, channel_id=channel.id, kind="red_packet", external_id="nope", ok=True, result={"ok": True}) is None


# ---------- 统计与明细 ----------

def test_ledger_stats_cover_today_week_and_all_time_and_filter_by_group(env) -> None:
    db, channel, _room = env
    now = now_shanghai()

    def add(external_id, status, fen, days_ago, chat="1@chatroom", kind="red_packet"):
        row = ledger.record_incoming(
            db, channel_id=channel.id, kind=kind, external_id=external_id, chat_type="group", chat_external_id=chat,
            chat_title="群", sender_id="wxid_x", sender_name="某人", amount_fen=fen,
        )
        row.status = status
        row.created_at = now - timedelta(days=days_ago)

    add("a", "received", 100, 0)
    add("b", "received", 250, 3)
    add("c", "missed", None, 0)
    add("d", "failed", None, 0)
    add("e", "received", 1000, 30)  # 一个月前:只进累计
    add("f", "received", 777, 0, chat="other@chatroom")  # 别的群
    add("g", "received", 500, 0, kind="transfer")  # 转账不混进红包统计
    db.commit()

    data = ledger.build_ledger(db, channel_id=channel.id, kind="red_packet", chat_external_id="1@chatroom")
    today, week, total = data["periods"]
    assert (today["received"], today["amount"], today["missed"], today["attention"]) == (1, "¥1.00", 1, 1)
    assert (week["received"], week["amount"]) == (2, "¥3.50")
    assert (total["received"], total["amount"], total["total"]) == (3, "¥13.50", 5)
    # 明细:最新在前,带状态文字
    # 明细按写入顺序倒排(最新写入的在前):e(已收到,一个月前) d(失败) c(来晚了) b(已收到) a(已收到);别的群和转账不在其中
    assert [item["status_label"] for item in data["rows"]] == ["已收到", "失败", "来晚了", "已收到", "已收到"]
    # 不限群:包含另一个群
    assert ledger.build_ledger(db, channel_id=channel.id, kind="red_packet")["periods"][-1]["received"] == 4


# ---------- 页面 ----------

def test_account_page_shows_the_transfer_ledger_collapsed_under_the_switch(webridge_channel) -> None:
    client, ids, channel_id = webridge_channel
    with ids["factory"]() as db:
        db.add(PaymentReceipt(
            tenant_id=ids["alpha"], channel_id=channel_id, kind="transfer", external_id="tx-9", chat_type="direct", chat_external_id="wxid_alice",
            chat_title="张三", sender_id="wxid_alice", sender_name="爱丽丝", amount_fen=1250, status="received",
        ))
        db.commit()
    _login(client, "admin")
    html = client.get(f"/channels/{channel_id}").text
    section = html.split("自动收款", 1)[1]
    assert "<details" in section and "收款明细与统计" in section and "累计收到 1 笔 · ¥12.50" in section
    assert "爱丽丝" in section and "wxid_alice" in section and "¥12.50" in section and "已收到" in section and "私聊" in section
    assert "<details class=\"pay-ledger\" open" not in html  # 默认折叠


def test_group_page_shows_the_red_packet_ledger_only_for_this_group(webridge_channel) -> None:
    client, ids, channel_id = webridge_channel
    with ids["factory"]() as db:
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel_id, external_id="1@chatroom", name="养生群", is_active=True)
        db.add(room)
        db.flush()
        for external_id, chat, name, fen in (("r1", "1@chatroom", "老板", 88), ("r2", "2@chatroom", "别群的人", 99)):
            db.add(PaymentReceipt(
                tenant_id=ids["alpha"], channel_id=channel_id, kind="red_packet", external_id=external_id, chat_type="group", chat_external_id=chat,
                chat_title="群", sender_id=f"wxid_{external_id}", sender_name=name, amount_fen=fen, status="received",
            ))
        db.commit()
        room_id = int(room.id)
    _login(client, "admin")
    html = client.get(f"/channels/{channel_id}/chatrooms/{room_id}").text
    section = html.split("自动抢红包", 1)[1]
    assert "收款明细与统计" in section and "累计收到 1 笔 · ¥0.88" in section and "老板" in section
    assert "别群的人" not in html  # 别的群的红包不会出现在这个群里


def test_a_non_duplicate_integrity_error_is_not_swallowed(env) -> None:
    """只有"同一笔已存在"才算重复;缺必填字段这类真正的错误必须抛出来。"""

    db, channel, _room = env
    with patch.object(db, "flush", side_effect=__import__("sqlalchemy").exc.IntegrityError("insert", {}, Exception("NOT NULL constraint failed"))):
        with pytest.raises(Exception, match="NOT NULL"):
            ledger.record_incoming(db, channel_id=channel.id, kind="transfer", external_id="x", chat_type="direct", chat_external_id="u")
