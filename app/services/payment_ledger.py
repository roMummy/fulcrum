"""收款记录:红包、转账的入库、结果回填、明细与统计。

数据来源只有两处:自动抢红包(群功能)和自动收款(账号开关)处理过的每一笔。
出现事件、决定处理时就写一行(状态 pending);手机回报命令结果后回填状态和金额。
用户在手机上手动领取的红包/转账系统无从得知,不在记录里。

金额:转账的事件自带金额。红包的领取回报只说明"已打开"(opened),没有金额;金额要在领取之后
再查一次红包详情(red_packet_detail)才知道:我领到的是 current_received_amount_cent,
红包已领完(remaining_count=0)而我是 0 就是来晚了。手机端打开红包有一点延迟,所以详情最多查几次(见 red_packet 模块)。
手机的原始回报(截断)都存进 result_json,便于核对。
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.time import now_shanghai
from app.models.entities import PaymentReceipt

KIND_RED_PACKET = "red_packet"
KIND_TRANSFER = "transfer"

STATUS_PENDING = "pending"
STATUS_RECEIVED = "received"
STATUS_MISSED = "missed"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_FAILED = "failed"

# (页面显示文字, 颜色倾向)
STATUS_LABELS: dict[str, tuple[str, str]] = {
    STATUS_PENDING: ("处理中", "muted"),
    STATUS_RECEIVED: ("已收到", "success"),
    STATUS_MISSED: ("来晚了", "outline"),
    STATUS_UNCONFIRMED: ("未确认到账", "warning"),
    STATUS_FAILED: ("失败", "danger"),
}

# 红包领取失败时,回报里出现这些词说明是"来晚了"(被领完/已过期),不是故障。
_MISSED_PATTERN = re.compile(r"claimed|empty|expired|finish|over|已被领|领完|抢光|过期|已领取", re.IGNORECASE)
# 转账确认的"结果未知"状态(文档:不要直接重试,应查询转账状态)。
_UNKNOWN_TRANSFER_STATUSES = {"confirm_invoked", "callback_unknown"}
RESULT_JSON_LIMIT = 2000
DETAIL_LIMIT = 50


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _yuan_to_fen(value: Any) -> int | None:
    """"￥0.02" / "0.02" / 0.02 -> 2;不是金额返回 None。"""

    cleaned = re.sub(r"[￥¥元,\s]", "", _text(value))
    if not cleaned:
        return None
    try:
        return int((Decimal(cleaned) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def extract_amount_fen(*sources: Any) -> int | None:
    """从事件/回报里提取金额(分)。优先整数分,其次元为单位的字符串;各来源及其一层嵌套(data/detail)里依次找。"""

    candidates: list[Mapping[str, Any]] = []
    for source in sources:
        if isinstance(source, Mapping):
            candidates.append(source)
            candidates.extend(value for key in ("data", "detail", "result") if isinstance(value := source.get(key), Mapping))
    for source in candidates:
        fen = source.get("amount_fen")
        if isinstance(fen, (int, float)) and not isinstance(fen, bool) and fen >= 0:
            return int(fen)
        if isinstance(fen, str) and fen.strip().isdigit():
            return int(fen.strip())
        for key in ("amount_value", "amount_yuan", "amount_text"):
            parsed = _yuan_to_fen(source.get(key))
            if parsed is not None:
                return parsed
    return None


def format_yuan(amount_fen: int | None) -> str:
    return "-" if amount_fen is None else f"¥{amount_fen / 100:,.2f}"


def record_incoming(
    db: Session,
    *,
    channel_id: int,
    kind: str,
    external_id: str,
    chat_type: str,
    chat_external_id: str,
    chat_title: str = "",
    sender_id: str = "",
    sender_name: str = "",
    amount_fen: int | None = None,
) -> PaymentReceipt | None:
    """写入一笔待处理记录。同一渠道里同一种、同一个 ID 已存在(事件重复投递)时返回 None。

    在保存点里写入:重复时只回滚这一行,不影响外层事务。
    """

    row = PaymentReceipt(
        channel_id=int(channel_id), kind=kind, external_id=_text(external_id)[:191], chat_type=chat_type or "group",
        chat_external_id=_text(chat_external_id)[:191], chat_title=_text(chat_title)[:200] or None,
        sender_id=_text(sender_id)[:191] or None, sender_name=_text(sender_name)[:200] or None,
        amount_fen=amount_fen, status=STATUS_PENDING,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # 只有确认是"已存在同一笔"才当作重复;其他完整性错误(例如缺少必填字段)是真正的问题,要抛出来,不能悄悄吞掉。
        duplicate = db.scalar(
            select(PaymentReceipt.id).where(
                PaymentReceipt.channel_id == row.channel_id, PaymentReceipt.kind == kind, PaymentReceipt.external_id == row.external_id
            )
        )
        if duplicate is None:
            raise
        return None
    return row


def classify(kind: str, *, ok: bool, inner: Mapping[str, Any]) -> str:
    """把手机回报归为一种状态。

    转账:confirmed=true 才算收到(文档:ok、accepted 都不代表到账);等待超时/结果未知是"未确认到账"。
    红包:领取成功只是"已打开",先记处理中,金额和结果等详情;失败时回报里带"已被领完/已过期"一类的词算"来晚了",其余算失败。
    """

    status = _text(inner.get("status"))
    if kind == KIND_TRANSFER:
        if inner.get("confirmed"):
            return STATUS_RECEIVED
        if ok or status in _UNKNOWN_TRANSFER_STATUSES:
            return STATUS_UNCONFIRMED
        return STATUS_FAILED
    if ok and inner.get("opened") is not False:
        # 红包领取回报只说明已打开,到底领到多少要等详情查询确认(见 apply_red_packet_detail)。
        return STATUS_PENDING
    return STATUS_MISSED if _MISSED_PATTERN.search(json.dumps(inner, ensure_ascii=False)) else STATUS_FAILED


def settle(db: Session, *, channel_id: int, kind: str, external_id: str, ok: bool, result: Mapping[str, Any]) -> PaymentReceipt | None:
    """手机回报命令结果后回填记录;找不到对应记录(例如功能上线前入队的命令)返回 None。调用方负责提交。"""

    row = db.scalar(
        select(PaymentReceipt).where(
            PaymentReceipt.channel_id == int(channel_id), PaymentReceipt.kind == kind, PaymentReceipt.external_id == _text(external_id)
        )
    )
    if row is None:
        return None
    inner = result.get("result") if isinstance(result.get("result"), Mapping) else result
    row.status = classify(kind, ok=ok, inner=inner)
    row.result_status = _text(inner.get("status"))[:60] or None
    row.result_json = json.dumps(result, ensure_ascii=False)[:RESULT_JSON_LIMIT]
    # 转账事件里已有金额,回报里有就以回报为准;红包金额只能从回报里取
    found = extract_amount_fen(result, inner)
    if found is not None:
        row.amount_fen = found
    row.settled_at = now_shanghai()
    return row


def apply_red_packet_detail(
    db: Session, *, channel_id: int, external_id: str, ok: bool, result: Mapping[str, Any], attempt: int, max_attempts: int
) -> str:
    """用红包详情回填领取结果。返回 "final"(已有定论或不用再查)或 "retry"(还没查到结果,稍后再查一次)。

    手机端的"打开红包"是异步的,刚打开时详情里我的金额可能还是 0,所以:
    - 我领到了(current_received_amount_cent > 0):已收到,金额就是它;
    - 红包已被领完(remaining_count = 0)而我是 0:来晚了;
    - 还有剩余、我是 0,或者详情没查到:没到最后一次就再查一次,最后一次仍没结果记"未确认到账"。
    调用方负责提交。
    """

    row = db.scalar(
        select(PaymentReceipt).where(
            PaymentReceipt.channel_id == int(channel_id), PaymentReceipt.kind == KIND_RED_PACKET, PaymentReceipt.external_id == _text(external_id)
        )
    )
    if row is None or row.status != STATUS_PENDING:
        return "final"
    inner = result.get("result") if isinstance(result.get("result"), Mapping) else result
    ready = ok and _text(inner.get("status")) == "detail_ready"
    mine = inner.get("current_received_amount_cent") if ready else None
    mine = int(mine) if isinstance(mine, (int, float)) and not isinstance(mine, bool) else None
    remaining = inner.get("remaining_count") if ready else None
    if mine:
        verdict = STATUS_RECEIVED
        row.amount_fen = mine
    elif ready and remaining == 0:
        verdict = STATUS_MISSED
    elif attempt >= max_attempts:
        verdict = STATUS_UNCONFIRMED
    else:
        return "retry"
    row.status = verdict
    row.result_status = _text(inner.get("status"))[:60] or None
    row.result_json = json.dumps(result, ensure_ascii=False)[:RESULT_JSON_LIMIT]
    row.settled_at = now_shanghai()
    return "final"


def _period_stats(db: Session, base_filters: list, since) -> dict[str, Any]:
    stmt = select(PaymentReceipt.status, func.count(PaymentReceipt.id), func.coalesce(func.sum(PaymentReceipt.amount_fen), 0)).where(*base_filters)
    if since is not None:
        stmt = stmt.where(PaymentReceipt.created_at >= since)
    counts: dict[str, tuple[int, int]] = {status: (int(n), int(total)) for status, n, total in db.execute(stmt.group_by(PaymentReceipt.status)).all()}
    received_count, received_fen = counts.get(STATUS_RECEIVED, (0, 0))
    return {
        "received": received_count,
        "amount": format_yuan(received_fen),
        "missed": counts.get(STATUS_MISSED, (0, 0))[0],
        # 需要人看一眼的:未确认到账 + 失败
        "attention": counts.get(STATUS_UNCONFIRMED, (0, 0))[0] + counts.get(STATUS_FAILED, (0, 0))[0],
        "total": sum(n for n, _ in counts.values()),
    }


def build_ledger(db: Session, *, channel_id: int, kind: str, chat_external_id: str | None = None, limit: int = DETAIL_LIMIT) -> dict[str, Any]:
    """页面用的统计(今日/近 7 天/累计)和最近明细。chat_external_id 不为空时只看这个群。"""

    filters = [PaymentReceipt.channel_id == int(channel_id), PaymentReceipt.kind == kind]
    if chat_external_id:
        filters.append(PaymentReceipt.chat_external_id == chat_external_id)
    now = now_shanghai()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    periods = [
        {"label": "今日", **_period_stats(db, filters, today)},
        {"label": "近 7 天", **_period_stats(db, filters, today - timedelta(days=6))},
        {"label": "累计", **_period_stats(db, filters, None)},
    ]
    rows = db.scalars(select(PaymentReceipt).where(*filters).order_by(PaymentReceipt.id.desc()).limit(max(1, int(limit)))).all()
    items = []
    for row in rows:
        label, tone = STATUS_LABELS.get(row.status, (row.status, "outline"))
        items.append(
            {
                "time": row.created_at,
                "sender_name": row.sender_name or "",
                "sender_id": row.sender_id or "",
                "chat_type": row.chat_type,
                "chat_title": row.chat_title or "",
                "amount": format_yuan(row.amount_fen),
                "status": row.status,
                "status_label": label,
                "tone": tone,
                "result_status": row.result_status or "",
            }
        )
    return {"kind": kind, "periods": periods, "rows": items, "total": periods[-1]["total"]}
