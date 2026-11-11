"""微桥账号资料(account_status)与账号昵称展示的轻量测试。"""

from __future__ import annotations

import json
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="account-profile-logs-"))

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, Channel, TaskJob
from app.services.account_identity import account_identity, bot_identity
from app.services.wechat_webridge import service

WXID = "wxid_demo0000000001"
STATUS = {"ok": True, "account_id": WXID, "user_name": WXID, "nick_name": "演示账号@rCodex", "avatar_url": "https://wx.qlogo.cn/a.png"}


@pytest.fixture()
def env():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with tenant_scope(1):
        with tenant_session(factory) as db:
            bot = Bot(tenant_id=1, name=WXID, status="running", is_enabled=True)
            db.add(bot)
            db.flush()
            runtime = {"account_connection": {"account_id": WXID, "display_name": WXID, "state": "active"}, "webridge": {"account_id": WXID}}
            channel = Channel(tenant_id=1, bot_id=bot.id, name=WXID, channel_type="wechat_webridge", status="online", is_enabled=True, runtime_info_json=json.dumps(runtime))
            db.add(channel)
            db.flush()
            yield db, bot, channel


def test_wxid_names_are_replaced_by_nickname(env) -> None:
    db, bot, channel = env
    service._save_account_profile(db, channel, STATUS)
    assert channel.name == "演示账号@rCodex" and bot.name == "演示账号@rCodex"
    runtime = json.loads(channel.runtime_info_json)
    assert runtime["account_connection"]["display_name"] == "演示账号@rCodex"
    assert runtime["account_profile"]["avatar_url"] == "https://wx.qlogo.cn/a.png"
    # 展示:昵称为主,wxid 作为弱化的第二行,头像取资料
    ident = account_identity(channel)
    assert (ident["name"], ident["external_id"], ident["show_id"]) == ("演示账号@rCodex", WXID, True)
    assert ident["avatar_url"] == "https://wx.qlogo.cn/a.png"
    assert bot_identity(bot.name, [channel])["external_id"] == WXID


def test_custom_names_are_kept_and_auto_names_follow_nickname_changes(env) -> None:
    db, bot, channel = env
    channel.name, bot.name = "客服一号", "我的机器人"  # 用户自己改过的名字
    service._save_account_profile(db, channel, STATUS)
    assert (channel.name, bot.name) == ("客服一号", "我的机器人")

    # 自动写入的名字,在账号改昵称后跟着更新
    channel.name, bot.name = WXID, WXID
    service._save_account_profile(db, channel, STATUS)
    service._save_account_profile(db, channel, {**STATUS, "nick_name": "新昵称"})
    assert (channel.name, bot.name) == ("新昵称", "新昵称")


def test_bot_name_conflict_and_empty_nickname_are_safe(env) -> None:
    db, bot, channel = env
    db.add(Bot(tenant_id=1, name="演示账号@rCodex", status="stopped", is_enabled=True))
    db.flush()
    service._save_account_profile(db, channel, STATUS)
    assert channel.name == "演示账号@rCodex" and bot.name == WXID  # 机器人名被占用时保持原名
    # 手机没回报昵称:什么都不改
    service._save_account_profile(db, channel, {"ok": True, "account_id": WXID, "nick_name": ""})
    assert channel.name == "演示账号@rCodex"


def test_enqueue_account_status_is_throttled(env) -> None:
    db, bot, channel = env

    def pending() -> int:
        db.flush()  # 夹具关闭了 autoflush,查询前显式刷出刚入队的任务
        return len(db.scalars(select(TaskJob).where(TaskJob.priority == service.ACCOUNT_STATUS_PRIORITY)).all())

    service._enqueue_account_status(db, channel)
    assert pending() == 1
    # 已有未完成任务 / 刚请求过:不重复入队
    service._enqueue_account_status(db, channel)
    assert pending() == 1
    # 资料已拿到且未过期:不再请求
    db.query(TaskJob).delete()
    service._save_account_profile(db, channel, STATUS)
    service._enqueue_account_status(db, channel)
    assert pending() == 0
    # 内部采样命令不算业务积压
    service._enqueue_account_status(db, channel)
    assert service.count_backlog(db, channel) == (0, 0)


def test_unfetched_profile_falls_back_to_channel_name(env) -> None:
    db, bot, channel = env
    ident = account_identity(channel)
    # 还没拿到昵称:显示名就是 wxid,不重复展示第二行
    assert ident["name"] == WXID and ident["show_id"] is False
