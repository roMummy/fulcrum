"""咕嘎助手渠道的安全边界:路径 Token 认证、日志脱敏、自发消息、命令领取隔离。

PR 自带的测试只覆盖服务层,没有任何用例验证 HTTP 路径里的渠道 Token(这是这个对外接入点唯一的认证)。
这里用真实的 FastAPI 路由补上,并固定审计中发现的三个问题:
Token 被明文写进日志、账号自己发的消息会再次进入 AI、同租户其他渠道的积压命令会挤掉本渠道命令。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="guga-security-logs-"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.api.guga_agent as guga_api
from app.core.db import Base
from app.core.logging import ContextFormatter
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel
from app.models.saas import Tenant
from app.services.channel_adapters.wechat_gugatool import WechatGugatoolAdapter
from app.services.guga_agent import service
from app.services.guga_agent.access import connection_url, initialize_connection

PUBLIC_BASE = "http://192.0.2.10:8280"
IDENTITY = {"device_id": "dev-1", "account_id": "wxid_self"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'guga-security.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    for name in ("SessionLocal", "TenantSessionLocal"):
        monkeypatch.setattr(guga_api, name, factory)
    with factory() as db:
        db.add_all([Tenant(id=7, code="t7", name="七号"), Tenant(id=8, code="t8", name="八号")])
        db.commit()
    channels = {}
    with tenant_scope(7), open_tenant_session(factory) as db:
        for key in ("a", "b"):
            channel = Channel(bot_id=1, name=f"咕嘎{key}", channel_type="wechat_gugatool", is_enabled=True, tenant_id=7)
            db.add(channel)
            db.flush()
            initialize_connection(channel, PUBLIC_BASE)
            channels[key] = (int(channel.id), connection_url(channel).rsplit("/", 1)[1])
        db.commit()
    app = FastAPI()
    app.include_router(guga_api.router)
    return TestClient(app), factory, channels


def _path(channel_id: int, token: str, suffix: str) -> str:
    return f"/api/guga-agent/{channel_id}/{token}/agent/{suffix}"


# ---------- Token 认证:对外接入点唯一的门 ----------

@pytest.mark.parametrize("method, suffix", [("post", "register"), ("post", "state"), ("post", "event"), ("post", "poll"), ("post", "command_result"), ("get", "health")])
def test_every_endpoint_rejects_missing_wrong_and_other_channels_tokens(env, method, suffix) -> None:
    client, _factory, channels = env
    (a_id, a_token), (_b_id, b_token) = channels["a"], channels["b"]
    call = lambda token: getattr(client, method)(_path(a_id, token, suffix), **({"json": IDENTITY} if method == "post" else {}))
    assert call("wrong-token").status_code == 403
    assert call("x" * 43).status_code == 403             # 形态对但不是它的 Token
    assert call(b_token).status_code == 403              # 另一个渠道的 Token 不能用在这个渠道上
    assert call(a_token).status_code != 403              # 自己的 Token 能通过认证(后续业务结果不在这里断言)


def test_unknown_channel_and_other_channel_types_look_the_same_as_a_bad_token(env) -> None:
    client, factory, channels = env
    assert client.post(_path(99999, "x" * 43, "poll"), json=IDENTITY).status_code == 404
    with tenant_scope(7), open_tenant_session(factory) as db:
        other = Channel(bot_id=1, name="别的渠道", channel_type="wechat_webridge", is_enabled=True, tenant_id=7)
        db.add(other)
        db.flush()
        other_id = int(other.id)
        db.commit()
    assert client.post(_path(other_id, "x" * 43, "poll"), json=IDENTITY).status_code == 404  # 不是咕嘎渠道:这个入口不接受


def test_a_channel_without_any_token_rejects_everything(env) -> None:
    client, factory, _channels = env
    with tenant_scope(7), open_tenant_session(factory) as db:
        bare = Channel(bot_id=1, name="没有 token", channel_type="wechat_gugatool", is_enabled=True, tenant_id=7)
        db.add(bare)
        db.flush()
        bare_id = int(bare.id)
        db.commit()
    for token in ("", "None", "null", "x" * 43):
        if token:
            assert client.post(_path(bare_id, token, "poll"), json=IDENTITY).status_code == 403  # 没有登记 token 的渠道,任何值都不通过


def test_the_websocket_refuses_a_wrong_token(env) -> None:
    client, _factory, channels = env
    a_id, _a_token = channels["a"]
    with pytest.raises(Exception):  # 握手阶段被拒绝(1008),不会进入 hello 流程
        with client.websocket_connect(f"/api/guga-agent/{a_id}/{'x' * 43}/agent/ws"):
            pass


# ---------- 日志:Token 不能明文留在日志里 ----------

def test_the_connection_token_is_hidden_in_logs_for_both_phone_protocols() -> None:
    token = "A" * 43
    formatter = ContextFormatter("%(message)s")
    for prefix in ("wechat-webridge", "guga-agent"):
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "request %s", (f"/api/{prefix}/2/{token}/agent/poll",), None)
        rendered = formatter.format(record)
        assert token not in rendered and "[已隐藏]" in rendered and f"/api/{prefix}/2/" in rendered, prefix
    ws = logging.LogRecord("t", logging.INFO, __file__, 1, "ws %s", (f"/api/guga-agent/2/{token}/agent/ws",), None)
    assert token not in formatter.format(ws)


# ---------- 自发消息:不进 AI ----------

def _adapter_event(sender: str, *, group: bool = False) -> dict:
    item = {"kind": "incoming", "fromUser": "room@chatroom" if group else sender, "toUser": "wxid_self", "realChatUser": sender if group else "",
            "messageType": 1, "content": "你好", "id": "m1"}
    return item


def test_messages_sent_by_the_registered_account_are_marked_so_the_ai_ignores_them(env) -> None:
    _client, factory, channels = env
    channel_id, _token = channels["a"]
    adapter = WechatGugatoolAdapter()
    with tenant_scope(7), open_tenant_session(factory) as db:
        channel = db.get(Channel, channel_id)
        service.register_device(db, channel, IDENTITY)
        mine = adapter.normalize_inbound_payload(channel, _adapter_event("wxid_self"))
        friend = adapter.normalize_inbound_payload(channel, _adapter_event("wxid_friend"))
        mine_in_group = adapter.normalize_inbound_payload(channel, _adapter_event("wxid_self", group=True))
        assert mine["details"].get("from_self") is True and mine_in_group["details"].get("from_self") is True
        assert "from_self" not in friend["details"]  # 朋友发来的照常
        fresh = Channel(bot_id=1, name="未注册", channel_type="wechat_gugatool", is_enabled=True, tenant_id=7)
        db.add(fresh)
        db.flush()
        assert "from_self" not in adapter.normalize_inbound_payload(fresh, _adapter_event("wxid_self"))["details"]  # 账号没登记:不猜


# ---------- 命令领取:只领本渠道的 ----------

def test_a_busy_neighbour_channel_cannot_starve_this_channels_commands(env) -> None:
    _client, factory, channels = env
    (a_id, _), (b_id, _) = channels["a"], channels["b"]
    with tenant_scope(7), open_tenant_session(factory) as db:
        a, b = db.get(Channel, a_id), db.get(Channel, b_id)
        registered = {}
        for channel in (a, b):
            registered[channel.id] = service.register_device(db, channel, IDENTITY)
        for index in range(60):  # 邻居渠道积压 60 条、优先级更高的命令
            service.enqueue_command(db, b, command_name="get_friend_list", params={}, request_id=f"b-{index}", priority=90)
        service.enqueue_command(db, a, command_name="get_friend_list", params={}, request_id="a-only", priority=10)
        db.commit()
        payload = {**IDENTITY, "session_id": registered[a.id]["session_id"]}
        claimed = service.claim_commands(db, a, payload, limit=5)
        ids = [item["request_id"] for item in claimed]
        # 注册时平台会自动给本渠道补一条通讯录同步命令,所以不止一条;关键是:自己的命令都领到了(没被邻居挤出窗口),一条邻居的都没有
        assert "a-only" in ids and not any(item.startswith("b-") for item in ids)
        neighbour = service.claim_commands(db, b, {**IDENTITY, "session_id": registered[b.id]["session_id"]}, limit=5)
        assert len(neighbour) == 5 and all(item["request_id"].startswith("b-") for item in neighbour)
