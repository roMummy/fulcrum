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
from app.services.wechat_webridge import service, wakeup
from tests.test_ai_chat import _session
from tests.test_ai_dispatch import _seed
from tests.test_group_transfer_receive import IDENTITY, _event as transfer_event
from test_provider_node_pages import _login, provider_page_context  # noqa: F401  (provider_page_context 是 fixture)

PACKET = "rp_msg_100"


@pytest.fixture()
def env(monkeypatch):
    from contextlib import contextmanager

    db = _session()
    channel = _seed(db)
    channel.runtime_info_json = json.dumps({"webridge": IDENTITY})
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=channel.id, external_id="1@chatroom", name="养生群", is_active=True)
    db.add_all([
        room,
        Conversation(id=3, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="1@chatroom", chat_type="group", title="养生群"),
        Conversation(id=4, tenant_id=1, bot_id=1, channel_id=channel.id, channel_type="wechat_webridge", external_id="wxid_alice", chat_type="direct", title="张三"),
    ])
    db.commit()
    save_state(room, "red_packet", enabled=True, config={"delay_ms": 0})
    save_state(room, "transfer_receive", enabled=True)
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


def test_transfer_event_is_recorded_with_amount_payer_and_group(env) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify"):
        dispatch_group_features(db, channel, transfer_event(sender="wxid_alice", amount_fen=2), conversation_id=3)
        dispatch_group_features(db, channel, transfer_event(sender="wxid_alice", amount_fen=2), conversation_id=3)  # 重复投递
    row = _receipts(db)[0]
    assert len(_receipts(db)) == 1
    assert (row.kind, row.external_id, row.status, row.amount_fen) == ("transfer", "tx-1", "pending", 2)  # 转账事件自带金额
    assert (row.sender_id, row.sender_name, row.chat_type, row.chat_external_id, row.chat_title) == ("wxid_alice", "爱丽丝", "group", "1@chatroom", "养生群")


# ---------- 结果回填 ----------

def _run(db, channel, request_id: str, reported: dict) -> None:
    for job in db.scalars(select(TaskJob).where(TaskJob.job_type == service.JOB_TYPE)).all():
        if json.loads(job.payload_json).get("command", {}).get("request_id") == request_id:
            job.status = "running"  # 手机已领取这条命令
    db.commit()
    with patch.object(service, "_enqueue_account_status"), patch.object(service, "_enqueue_device_status"):
        service.accept_command_result(db, channel, {**IDENTITY, "request_id": request_id, **reported})


def test_transfer_confirmation_settles_the_record(env) -> None:
    db, channel, _room = env
    with patch.object(wakeup, "notify"):
        dispatch_group_features(db, channel, transfer_event(sender="wxid_alice"), conversation_id=3)
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
        dispatch_group_features(db, channel, transfer_event(sender="wxid_alice"), conversation_id=3)
    _run(db, channel, "transfer_tx-1", reported)
    assert _receipts(db)[0].status == status


# 线上真实回报(2026-11-10 在 BOT-测试 群领到的 1 元红包):领取回报只说"已打开",没有金额;
# 金额在随后查询的红包详情里:current_received_amount_cent 是"我领到的"。
REAL_CLAIM = {
    "ok": True,
    "result": {"room_id": "49740991276@chatroom", "accepted": True, "red_packet_id": "rp_msg_3451183832849212577", "opened": True, "status": "opened"},
}
REAL_DETAIL = {
    "ok": True,
    "result": {
        "status": "detail_ready", "red_packet_id": "rp_msg_3451183832849212577", "total_amount_cent": 100, "total_count": 2, "received_count": 2,
        "remaining_count": 0, "remaining_amount_cent": 0, "current_received_amount_cent": 76, "current_received_amount": 0.76,
    },
}


@pytest.fixture()
def packet_flow(env, monkeypatch):
    """红包领取全流程的小工具:领取入队、手机回报领取结果、手机回报详情;并记录安排了哪几次详情查询。"""

    db, channel, _room = env
    scheduled: list[tuple[float, str, tuple]] = []
    monkeypatch.setattr(red_packet, "_schedule", lambda delay, func, *args: scheduled.append((delay, func.__name__, args)))
    monkeypatch.setattr(red_packet, "tenant_session", _same_session(db))

    def start_packet(packet: str) -> None:
        dispatch_group_features(db, channel, _packet_event(packet=packet), conversation_id=3)
        with patch.object(wakeup, "notify"):
            red_packet.claim(1, channel.id, 3, packet)

    def detail(packet: str, attempt: int, reported: dict) -> None:
        with patch.object(wakeup, "notify"):
            red_packet._enqueue_detail(1, channel.id, packet, attempt)
        _run(db, channel, red_packet.detail_request_id(packet, attempt), reported)

    return db, channel, scheduled, start_packet, detail


def _row(db, packet: str) -> PaymentReceipt:
    return next(row for row in _receipts(db) if row.external_id == packet)


def test_a_successful_claim_only_means_opened_so_the_amount_comes_from_the_detail(packet_flow) -> None:
    db, channel, scheduled, start_packet, detail = packet_flow
    start_packet("rp_ok")
    scheduled.clear()  # 去掉"延迟领取"的定时器,下面只看领取成功之后安排了什么
    _run(db, channel, "redpacket_rp_ok", REAL_CLAIM)
    # 领取回报没有金额:先是处理中,并安排第一次详情查询(2 秒后)
    assert (_row(db, "rp_ok").status, _row(db, "rp_ok").amount_fen) == ("pending", None)
    assert [(delay, name, args) for delay, name, args in scheduled] == [(2.0, "_enqueue_detail", (1, channel.id, "rp_ok", 1))]
    # 详情里我领到了 0.76 元
    scheduled.clear()
    detail("rp_ok", 1, REAL_DETAIL)
    row = _row(db, "rp_ok")
    assert (row.status, row.amount_fen, row.result_status) == ("received", 76, "detail_ready") and scheduled == []
    assert "current_received_amount_cent" in row.result_json  # 原始回报留存


def test_detail_shows_the_packet_was_fully_claimed_by_others(packet_flow) -> None:
    db, channel, scheduled, start_packet, detail = packet_flow
    start_packet("rp_late")
    _run(db, channel, "redpacket_rp_late", REAL_CLAIM)
    scheduled.clear()
    late = {"ok": True, "result": {**REAL_DETAIL["result"], "current_received_amount_cent": 0, "current_received_amount": 0}}
    detail("rp_late", 1, late)
    assert (_row(db, "rp_late").status, _row(db, "rp_late").amount_fen) == ("missed", None) and scheduled == []


def test_detail_is_retried_when_the_phone_has_not_finished_opening_and_gives_up_at_the_end(packet_flow) -> None:
    db, channel, scheduled, start_packet, detail = packet_flow
    start_packet("rp_slow")
    _run(db, channel, "redpacket_rp_slow", REAL_CLAIM)
    scheduled.clear()
    # 还有剩余、我还是 0:手机端可能还没处理完,再查一次(2s → 4s → 8s,共 3 次)
    not_yet = {"ok": True, "result": {**REAL_DETAIL["result"], "current_received_amount_cent": 0, "remaining_count": 1}}
    detail("rp_slow", 1, not_yet)
    assert _row(db, "rp_slow").status == "pending" and scheduled == [(4.0, "_enqueue_detail", (1, channel.id, "rp_slow", 2))]
    scheduled.clear()
    detail("rp_slow", 2, {"ok": False, "result": {"ok": False, "error": "timeout"}})  # 查询本身失败也重试
    assert _row(db, "rp_slow").status == "pending" and scheduled == [(8.0, "_enqueue_detail", (1, channel.id, "rp_slow", 3))]
    scheduled.clear()
    detail("rp_slow", 3, not_yet)  # 最后一次仍没结果:不再重试,记"未确认到账"
    assert _row(db, "rp_slow").status == "unconfirmed" and scheduled == []


def test_a_retry_that_finally_sees_the_amount_still_settles(packet_flow) -> None:
    db, channel, scheduled, start_packet, detail = packet_flow
    start_packet("rp_2nd")
    _run(db, channel, "redpacket_rp_2nd", REAL_CLAIM)
    detail("rp_2nd", 1, {"ok": True, "result": {**REAL_DETAIL["result"], "current_received_amount_cent": 0, "remaining_count": 1}})
    detail("rp_2nd", 2, REAL_DETAIL)
    assert (_row(db, "rp_2nd").status, _row(db, "rp_2nd").amount_fen) == ("received", 76)


def test_failed_claims_never_schedule_a_detail_query(packet_flow) -> None:
    db, channel, scheduled, start_packet, detail = packet_flow
    for packet in ("rp_gone", "rp_broken"):
        start_packet(packet)
    scheduled.clear()
    _run(db, channel, "redpacket_rp_gone", {"ok": False, "result": {"ok": False, "status": "fully_claimed"}})  # 来晚了
    _run(db, channel, "redpacket_rp_broken", {"ok": False, "result": {"ok": False, "status": "not_logged_in"}})
    assert (_row(db, "rp_gone").status, _row(db, "rp_broken").status) == ("missed", "failed") and scheduled == []


def test_detail_for_an_unknown_or_already_settled_packet_is_harmless(packet_flow) -> None:
    db, channel, scheduled, start_packet, detail = packet_flow
    assert ledger.apply_red_packet_detail(db, channel_id=channel.id, external_id="nope", ok=True, result=REAL_DETAIL, attempt=1, max_attempts=3) == "final"
    start_packet("rp_done")
    _run(db, channel, "redpacket_rp_done", REAL_CLAIM)
    detail("rp_done", 1, REAL_DETAIL)
    # 已经有定论,后到的详情不再改动
    other = {"ok": True, "result": {**REAL_DETAIL["result"], "current_received_amount_cent": 999}}
    assert ledger.apply_red_packet_detail(db, channel_id=channel.id, external_id="rp_done", ok=True, result=other, attempt=2, max_attempts=3) == "final"
    assert _row(db, "rp_done").amount_fen == 76


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

@pytest.fixture()
def webridge_channel(provider_page_context):  # noqa: F811
    client, ids = provider_page_context
    from app.models.entities import Channel as ChannelRow

    with ids["factory"]() as db:
        channel = ChannelRow(tenant_id=ids["alpha"], bot_id=ids["bot"], name="微桥账号", channel_type="wechat_webridge", is_enabled=True)
        db.add(channel)
        db.commit()
        return client, ids, int(channel.id)


def _group_page(webridge_channel, rows):
    """建一个群和若干收款记录,返回群页面 HTML。rows: (kind, external_id, chat, sender_name, fen)"""

    client, ids, channel_id = webridge_channel
    with ids["factory"]() as db:
        room = Chatroom(tenant_id=ids["alpha"], bot_id=ids["bot"], channel_id=channel_id, external_id="1@chatroom", name="养生群", is_active=True)
        db.add(room)
        db.flush()
        for kind, external_id, chat, name, fen in rows:
            db.add(PaymentReceipt(
                tenant_id=ids["alpha"], channel_id=channel_id, kind=kind, external_id=external_id, chat_type="group", chat_external_id=chat,
                chat_title="群", sender_id=f"wxid_{external_id}", sender_name=name, amount_fen=fen, status="received",
            ))
        db.commit()
        room_id = int(room.id)
    _login(client, "admin")
    return client.get(f"/channels/{channel_id}/chatrooms/{room_id}").text


def _card(html: str, key: str) -> str:
    """群功能页里某个功能卡片的 HTML(从它的 form 开始,到下一张卡片之前),与卡片先后顺序无关。"""

    start = html.index(f'data-gf-key="{key}"')
    nxt = html.find("data-gf-card", start)
    return html[start: nxt if nxt != -1 else len(html)]


def test_group_page_shows_the_red_packet_ledger_only_for_this_group(webridge_channel) -> None:
    html = _group_page(webridge_channel, [("red_packet", "r1", "1@chatroom", "老板", 88), ("red_packet", "r2", "2@chatroom", "别群的人", 99)])
    section = _card(html, "red_packet")
    assert "收款明细与统计" in section and "累计收到 1 笔 · ¥0.88" in section and "老板" in section
    assert "别群的人" not in html  # 别的群的红包不会出现在这个群里


def test_group_page_shows_the_transfer_ledger_under_the_auto_receive_card_and_not_mixed_with_red_packets(webridge_channel) -> None:
    html = _group_page(webridge_channel, [("transfer", "t1", "1@chatroom", "爱丽丝", 1250), ("red_packet", "r1", "1@chatroom", "老板", 88), ("transfer", "t2", "2@chatroom", "别群的人", 500)])
    section = _card(html, "transfer_receive")
    assert "收款明细与统计" in section and "累计收到 1 笔 · ¥12.50" in section and "爱丽丝" in section
    assert "老板" not in section and "别群的人" not in html
    assert "<details class=\"pay-ledger disclosure\" open" not in html  # 默认折叠


def test_a_non_duplicate_integrity_error_is_not_swallowed(env) -> None:
    """只有"同一笔已存在"才算重复;缺必填字段这类真正的错误必须抛出来。"""

    db, channel, _room = env
    with patch.object(db, "flush", side_effect=__import__("sqlalchemy").exc.IntegrityError("insert", {}, Exception("NOT NULL constraint failed"))):
        with pytest.raises(Exception, match="NOT NULL"):
            ledger.record_incoming(db, channel_id=channel.id, kind="transfer", external_id="x", chat_type="direct", chat_external_id="u")
