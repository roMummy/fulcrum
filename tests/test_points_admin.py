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
from app.services.points_admin import adjust_points, build_points_accounts, build_points_ledger, find_group_member

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


def test_find_member_by_nickname_wxid_and_ambiguity(db) -> None:
    assert find_group_member(db, GROUP, "小红")[0] == "wxid_b"
    assert find_group_member(db, GROUP, "wxid_a")[0] == "wxid_a"
    # “小明”是“小明二号”的子串但有一个精确同名,应只按唯一匹配;两个都含“小”则要求改用微信号。
    uid, hint = find_group_member(db, GROUP, "小")
    assert uid is None and "微信号" in hint
    assert find_group_member(db, GROUP, "不存在")[0] is None


def test_grant_deduct_and_ledger_show_operator_and_nickname(db) -> None:
    result = adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=50, operator="liuwei", note="活动奖励")
    assert result.ok and result.account["total_points"] == 50
    assert adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=-20, operator="liuwei").account["total_points"] == 30
    # 余额不足的扣减被拒绝,不产生负余额。
    assert adjust_points(db, group_id=GROUP, user_id="wxid_b", amount=-999, operator="liuwei").ok is False

    ledger = build_points_ledger(db, group_id=GROUP)
    assert [(r["delta"], r["balance_after"], r["source_label"]) for r in ledger["rows"]] == [(-20, 30, "人工调整"), (50, 50, "人工调整")]
    assert ledger["rows"][1]["identity"]["name"] == "小红" and ledger["rows"][1]["operator"] == "liuwei"
    assert build_points_ledger(db, keyword="小红")["paging"]["total"] == 2
    assert build_points_ledger(db, source="checkin")["paging"]["total"] == 0

    accounts = build_points_accounts(db, group_id=GROUP)
    assert accounts["summary"] == {"accounts": 1, "points": 30}
    assert accounts["rows"][0]["identity"]["name"] == "小红" and accounts["rows"][0]["group"]["name"] == "运营群"


def test_same_form_token_only_records_once_and_unknown_member_rejected(db) -> None:
    first = adjust_points(db, group_id=GROUP, user_id="wxid_a", amount=10, operator="liuwei", request_id="tok1")
    again = adjust_points(db, group_id=GROUP, user_id="wxid_a", amount=10, operator="liuwei", request_id="tok1")
    assert first.ok and again.ok
    assert PointsService(db).get_user_points("wxid_a", GROUP)["total_points"] == 10
    # 不在群成员里、也没有账户的人不能凭空发放。
    assert adjust_points(db, group_id=GROUP, user_id="wxid_ghost", amount=10, operator="liuwei").ok is False
    assert adjust_points(db, group_id=GROUP, user_id="wxid_a", amount=0, operator="liuwei").ok is False
