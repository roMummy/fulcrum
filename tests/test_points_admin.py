"""积分管理后台的轻量测试:账户列表、账单、人工发放/扣减、重名保护与重复提交幂等。"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="points-admin-logs-"))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Chatroom, ChatroomMember, PointsAccount, PointsTransaction, WorkWechatContact
from app.services.points import PointsService
from app.services.points_admin import adjust_points, build_points_accounts, build_points_ledger, group_options

GROUP = "g1@chatroom"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    for table in (Chatroom, ChatroomMember, PointsAccount, PointsTransaction, WorkWechatContact):
        table.__table__.create(engine)
    # 在租户作用域内建会话,写入时自动带 tenant_id。
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    scope = tenant_scope(1)
    scope.__enter__()
    ctx = tenant_session(factory)
    session = ctx.__enter__()
    room = Chatroom(tenant_id=1, bot_id=1, channel_id=1, external_id=GROUP, name="运营群", management_status="enabled")
    session.add(room)
    session.flush()
    for wxid, nick in (("wxid_a", "小明"), ("wxid_b", "小红"), ("wxid_c", "小明二号")):
        session.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=1, member_external_id=wxid, nickname=nick))
    session.commit()
    yield session
    ctx.__exit__(None, None, None)
    scope.__exit__(None, None, None)


def test_selecting_group_lists_all_members_including_zero_points(db) -> None:
    """选了群后列出全部成员,没有积分的成员显示 0,同样可以被选中发放;有积分的排前面。"""

    adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=7, operator="admin_user")
    result = build_points_accounts(db, group_id=GROUP)
    assert [(r["identity"]["name"], r["points"]) for r in result["rows"]] == [("小红", 7), ("小明", 0), ("小明二号", 0)]
    assert result["summary"] == {"accounts": 1, "points": 7}
    assert [r["identity"]["name"] for r in build_points_accounts(db, group_id=GROUP, keyword="二号")["rows"]] == ["小明二号"]
    # 没选群时只列已有积分账户。
    assert [r["identity"]["name"] for r in build_points_accounts(db)["rows"]] == ["小红"]


def test_grant_deduct_and_ledger_show_operator_and_nickname(db) -> None:
    result = adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=50, operator="admin_user", note="活动奖励")
    assert result.ok and result.account["total_points"] == 50
    assert adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=-20, operator="admin_user").account["total_points"] == 30
    # 余额不足的扣减被拒绝,不产生负余额。
    assert adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=-999, operator="admin_user").ok is False

    ledger = build_points_ledger(db, group_id=GROUP)
    assert [(r["delta"], r["balance_after"], r["source_label"]) for r in ledger["rows"]] == [(-20, 30, "人工调整"), (50, 50, "人工调整")]
    assert ledger["rows"][1]["identity"]["name"] == "小红" and ledger["rows"][1]["operator"] == "admin_user"
    assert build_points_ledger(db, keyword="小红")["paging"]["total"] == 2
    assert build_points_ledger(db, source="checkin")["paging"]["total"] == 0

    accounts = build_points_accounts(db, group_id=GROUP)
    assert accounts["summary"] == {"accounts": 1, "points": 30}
    assert accounts["rows"][0]["identity"]["name"] == "小红" and accounts["rows"][0]["group"]["name"] == "运营群"


def test_same_form_token_only_records_once_and_unknown_member_rejected(db) -> None:
    first = adjust_points(db, group_id=GROUP, user_id="wxid_a", amount=10, operator="admin_user", request_id="tok1")
    again = adjust_points(db, group_id=GROUP, user_id="wxid_a", amount=10, operator="admin_user", request_id="tok1")
    assert first.ok and again.ok
    assert PointsService(db).get_user_points("wxid_a", GROUP)["total_points"] == 10
    # 不在群成员里、也没有账户的人不能凭空发放。
    assert adjust_points(db, group_id=GROUP, user_id="wxid_ghost", amount=10, operator="admin_user").ok is False
    assert adjust_points(db, group_id=GROUP, user_id="wxid_a", amount=0, operator="admin_user").ok is False


def test_group_options_include_enabled_group_without_any_points(db) -> None:
    """没人有积分的已纳管群也能选到,否则无法发放第一笔积分。"""

    assert [item["name"] for item in group_options(db)] == ["运营群"]


def test_adjust_audit_log_target_id_is_integer_account_id(db) -> None:
    """回归:审计 target_id 是整数列,线上曾因写入 wxid 字符串触发 MariaDB 1366 导致提交报 500。

    这里复现路由里的审计调用方式:用账户主键作 target_id,成员 wxid 放进 detail。
    """

    from app.models.entities import AuditLog
    from app.services.ops import record_audit

    AuditLog.__table__.create(db.get_bind(), checkfirst=True)
    result = adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=5, operator="admin_user")
    item = record_audit(db, "points.adjust", "points_account", (result.account or {}).get("id"), "group=g, user=wxid_b, amount=5")
    assert isinstance(item.target_id, int) and "wxid_b" in item.detail
