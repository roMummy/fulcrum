from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="webridge-test-logs-"))

from app.models.entities import Channel, Conversation
from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter
from app.services.channel_catalog import get_channel_type_definition
from app.services.wechat_webridge import service


def test_webridge_normalizes_nested_media_and_payment_events() -> None:
    """嵌套事件必须保留消息类型、媒体元数据和支付标识，供统一事件层继续处理。"""

    channel = Channel(id=7, bot_id=1, name="微桥", channel_type="wechat_webridge")
    normalized = WechatWeBridgeAdapter().normalize_inbound_payload(
        channel,
        {
            "event_name": "message",
            "event_id": "evt-image-1",
            "body": {
                "msg_type": "image",
                "chat_id": "wxid-room",
                "from": "wxid-alice",
                "message_id": "msg-1",
                "image_md5": "md5-1",
                "image_aes_key": "aes-1",
            },
        },
    )

    assert normalized["event_key"] == "evt-image-1"
    assert normalized["message"]["message_type"] == "image"
    assert normalized["details"]["media"]["image_md5"] == "md5-1"

    payment = WechatWeBridgeAdapter().normalize_inbound_payload(
        channel,
        {"event_name": "transfer_received", "body": {"chat_id": "wxid-alice", "transfer_id": "tx-1", "amount_fen": 100}},
    )
    assert payment["message"]["message_type"] == "transfer"
    assert payment["details"]["payment"]["transfer_id"] == "tx-1"


def test_webridge_outbound_message_mapping_covers_media_and_cards(monkeypatch) -> None:
    """统一出站消息类型应转换为文档规定的手机命令和参数名。"""

    captured: list[dict] = []

    def fake_enqueue_job(db, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id=42)

    monkeypatch.setattr(service, "enqueue_job", fake_enqueue_job)
    channel = Channel(id=9, bot_id=1, tenant_id=3, name="微桥", channel_type="wechat_webridge")
    conversation = Conversation(id=11, bot_id=1, channel_id=9, external_id="wxid-bob", chat_type="direct", tenant_id=1)

    service.enqueue_message_command(
        object(), channel, conversation, {"message_type": "image", "image_url": "https://example.test/a.jpg"}
    )
    service.enqueue_message_command(
        object(), channel, conversation, {"message_type": "link", "title": "文档", "link_url": "https://example.test"}
    )

    assert captured[0]["payload"]["command"]["type"] == "send_image"
    assert captured[0]["payload"]["command"]["params"]["source"].endswith("a.jpg")
    assert captured[1]["payload"]["command"]["type"] == "send_link_card"
    assert captured[1]["payload"]["command"]["params"]["url"] == "https://example.test"


def test_webridge_media_upload_command_contains_attachment_reference(monkeypatch) -> None:
    """媒体附件任务必须携带附件 ID，手机回传时才能完成正确的幂等落库。"""

    captured: list[dict] = []

    def fake_enqueue_job(db, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id=88)

    monkeypatch.setattr(service, "enqueue_job", fake_enqueue_job)
    channel = Channel(id=4, bot_id=1, tenant_id=2, name="微桥", channel_type="wechat_webridge")
    conversation = Conversation(id=5, bot_id=1, channel_id=4, external_id="wxid-room", chat_type="group", tenant_id=1)
    service.enqueue_media_upload_command(
        object(),
        channel=channel,
        conversation=conversation,
        message_id="msg-88",
        media_type="video",
        upload_url="/api/wechat-webridge/4/agent/upload_media",
        attachment_id=99,
    )

    command = captured[0]["payload"]["command"]
    assert command["type"] == "upload_message_media"
    assert command["params"] == {
        "message_id": "msg-88",
        "media_type": "video",
        "upload_url": "/api/wechat-webridge/4/agent/upload_media",
        "attachment_id": 99,
    }


def test_webridge_command_catalog_exposes_groups_and_runtime_capabilities() -> None:
    """能力页同时提供稳定命令目录和手机注册时实际上报的能力。"""

    channel = Channel(
        id=12,
        bot_id=1,
        channel_type="wechat_webridge",
        runtime_info_json=json.dumps({"webridge": {"online": True, "capabilities": ["moments_list"]}}),
    )
    catalog = service.command_catalog(channel)
    assert "moments_list" in catalog["groups"]["moments"]
    assert catalog["reported_capabilities"] == ["moments_list"]
    assert catalog["online"] is True


def test_webridge_channel_catalog_advertises_advanced_business_domains() -> None:
    """渠道目录要把已接通的高级命令域展示给控制台，而不是只标记 send/inbound。"""

    capabilities = get_channel_type_definition("wechat_webridge").capabilities
    assert {"advanced_commands", "contacts", "payments", "moments", "favorites"}.issubset(capabilities)


class _FakeDirectoryDB:
    """只记录 add 的对象，查询一律视为不存在，用于验证目录字段映射而不依赖数据库夹具。"""

    def __init__(self) -> None:
        self.added: list = []

    def scalar(self, *_args, **_kwargs):
        return None

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None


def test_persist_directory_result_reads_group_id_and_nick_name() -> None:
    """groups_all 的群 ID 在 account_id、群名在 nick_name，不能因字段名不同而整条丢弃。"""

    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge")
    db = _FakeDirectoryDB()
    counts = service.persist_directory_result(
        db,
        channel,
        {"type": "groups_all"},
        {"items": [{"account_id": "45317011307@chatroom", "nick_name": "4K-养生交流群", "avatar_url": "https://example.test/a.png"}]},
    )
    assert counts["groups"] == 1
    room = db.added[0]
    assert room.external_id == "45317011307@chatroom"
    assert room.name == "4K-养生交流群"

    db = _FakeDirectoryDB()
    service.persist_directory_result(
        db, channel, {"type": "friends_all"}, {"items": [{"account_id": "wxid_a", "nick_name": "小柒", "remark": "", "display_name": ""}]}
    )
    assert db.added[0].name == "小柒"
    assert db.added[0].display_name == "小柒"


def test_reap_stale_commands_only_fails_timed_out_jobs_of_this_channel(tmp_path) -> None:
    """领取后超时未回报的命令要置为失败终态；未超时和其他渠道的任务不能被误伤。"""

    from datetime import timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.time import now_shanghai
    from app.models.entities import TaskJob

    engine = create_engine(f"sqlite:///{tmp_path / 'reap.db'}")
    TaskJob.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    now = now_shanghai()
    expired = now - timedelta(seconds=service.COMMAND_RUNNING_TIMEOUT_SECONDS + 60)

    def add_job(channel_id: int, locked_at, status: str = "running") -> TaskJob:
        job = TaskJob(
            tenant_id=1, job_type=service.JOB_TYPE, status=status, locked_at=locked_at,
            payload_json=json.dumps({"channel_id": channel_id, "command": {"type": "room_members"}}),
        )
        db.add(job)
        db.commit()
        return job

    stale = add_job(2, expired)
    fresh = add_job(2, now - timedelta(seconds=10))
    other_channel = add_job(3, expired)
    pending = add_job(2, None, status="pending")

    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge")
    assert service.reap_stale_commands(db, channel) == 1

    db.refresh(stale)
    assert stale.status == "failed"
    assert stale.error_message == "webridge_command_timeout"
    assert [db.get(TaskJob, job.id).status for job in (fresh, other_channel, pending)] == ["running", "running", "pending"]
    # 再次回收不应重复处理已经失败的任务。
    assert service.reap_stale_commands(db, channel) == 0


def test_claim_commands_plain_outbound_carries_command_name_and_args(tmp_path) -> None:
    """插件自动回复（无 command 的普通出站）下发给手机时必须带 command_name/args。"""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.entities import TaskJob

    engine = create_engine(f"sqlite:///{tmp_path / 'claim.db'}")
    TaskJob.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(TaskJob(
        tenant_id=1, job_type=service.JOB_TYPE, status="pending", priority=100,
        payload_json=json.dumps({"channel_id": 2, "payload": {"message_type": "text", "receiver_id": "r@chatroom", "content": "你好"}}),
    ))
    db.commit()
    identity = {"device_id": "d1", "session_id": "s1", "account_id": "a1"}
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge",
                      runtime_info_json=json.dumps({"webridge": identity}))

    commands = service.claim_commands(db, channel, identity)

    assert len(commands) == 1
    assert commands[0]["command_name"] == "send_message"
    assert commands[0]["args"] == {"receiver": "r@chatroom", "content": "你好"}


def test_accept_command_result_matches_request_id_strictly(tmp_path) -> None:
    """命令回报必须严格按 request_id 匹配，不能被无 command 的普通出站任务或其他渠道任务吞掉。"""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.entities import TaskJob

    engine = create_engine(f"sqlite:///{tmp_path / 'accept.db'}")
    TaskJob.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    def add_job(payload: dict, status: str) -> TaskJob:
        job = TaskJob(tenant_id=1, job_type=service.JOB_TYPE, status=status, payload_json=json.dumps(payload))
        db.add(job)
        db.commit()
        return job

    # 先放入已失败的普通出站任务（无 command）和其他渠道的同名命令，它们都不能抢走回报。
    plain_failed = add_job({"channel_id": 2, "payload": {"content": "x"}}, "failed")
    other_channel = add_job({"channel_id": 3, "command": {"request_id": "cmd_x", "type": "room_members"}}, "running")
    target = add_job({"channel_id": 2, "command": {"request_id": "cmd_x", "type": "room_members", "params": {}}}, "running")

    identity = {"device_id": "d1", "session_id": "s1", "account_id": "a1"}
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge",
                      runtime_info_json=json.dumps({"webridge": identity}))

    result = service.accept_command_result(db, channel, {**identity, "request_id": "cmd_x", "ok": True, "result": {"ok": True, "members": []}})

    assert result["job_id"] == target.id
    assert db.get(TaskJob, target.id).status == "completed"
    assert db.get(TaskJob, other_channel.id).status == "running"
    assert db.get(TaskJob, plain_failed.id).status == "failed"


def test_persist_room_members_maps_owner_admin_roles() -> None:
    """room_members 返回的成员项带 is_owner / is_admin，必须落成 owner / admin / member 角色。"""

    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge")
    room = SimpleNamespace(id=5, external_id="r@chatroom", member_count=0, last_synced_at=None)

    class _DB(_FakeDirectoryDB):
        """让群查询命中固定群，成员查询返回空以便新建。"""

        def scalar(self, stmt):
            return room if "chatrooms" in str(stmt) else None

    db = _DB()
    counts = service.persist_directory_result(
        db, channel, {"type": "room_members", "params": {"room_id": "r@chatroom"}},
        {"members": [
            {"account_id": "a", "nick_name": "群主", "is_owner": True, "is_admin": True},
            {"account_id": "b", "nick_name": "管理", "is_owner": False, "is_admin": True},
            {"account_id": "c", "nick_name": "成员", "is_owner": False, "is_admin": False},
        ]},
    )
    assert counts["members"] == 3
    assert [m.role for m in db.added] == ["owner", "admin", "member"]


def test_compact_result_for_storage_keeps_small_and_summarizes_large() -> None:
    """小结果原样保存；大群成员列表超过 TEXT 上限前只保留标量摘要，明细已落业务表。"""

    small = {"ok": True, "result": {"members": [{"account_id": "a"}]}}
    assert service._compact_result_for_storage(small, {"members": 1}) == small

    big = {"ok": True, "request_id": "r1", "result": {"room_id": "r", "member_count": 500, "members": [{"nick_name": "好" * 200}] * 400}}
    compact = service._compact_result_for_storage(big, {"members": 400})
    assert compact["request_id"] == "r1"
    assert compact["result"]["truncated"] is True
    assert compact["result"]["persisted_counts"] == {"members": 400}
    assert compact["result"]["member_count"] == 500
    assert "members" not in compact["result"]
    assert len(json.dumps(compact, ensure_ascii=False).encode("utf-8")) < service.RESULT_INLINE_LIMIT_BYTES


def test_group_scope_channel_types_include_webridge() -> None:
    """微桥渠道必须具备群级插件作用域能力，否则群详情里的启停接口会返回 400/404。"""

    from app.main import GROUP_SCOPE_CHANNEL_TYPES

    assert "wechat_webridge" in GROUP_SCOPE_CHANNEL_TYPES


def test_dashboard_message_filters_by_conversation_and_sender() -> None:
    """消息标签支持按会话（群/好友）和发送者筛选，且只返回本渠道数据。"""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.models.entities import Chatroom, ChatroomMember, Message, WorkWechatContact
    from app.services.wechat_webridge.dashboard import build_webridge_dashboard

    engine = create_engine("sqlite://")
    # 只建总览统计和消息查询用到的表，避免触发全库夹具。
    for model in (Channel, Conversation, Message, Chatroom, ChatroomMember, WorkWechatContact):
        model.__table__.create(engine, checkfirst=True)
    with Session(engine) as db:
        channel = Channel(id=1, bot_id=1, name="微桥", channel_type="wechat_webridge", tenant_id=1)
        room = Conversation(id=10, bot_id=1, channel_id=1, channel_type="wechat_webridge", external_id="r1@chatroom", chat_type="group", tenant_id=1)
        friend = Conversation(id=11, bot_id=1, channel_id=1, channel_type="wechat_webridge", external_id="wxid_f", chat_type="direct", tenant_id=1)
        other = Conversation(id=12, bot_id=1, channel_id=2, channel_type="wechat_webridge", external_id="x", chat_type="direct", tenant_id=1)
        db.add_all([channel, room, friend, other])
        db.add_all(
            [
                Message(conversation_id=10, direction="in", sender_id="wxid_a", sender_name="小明", content="群消息", tenant_id=1),
                Message(conversation_id=11, direction="in", sender_id="wxid_f", sender_name="老王", content="私聊", tenant_id=1),
                Message(conversation_id=12, direction="in", sender_id="wxid_z", sender_name="外渠道", content="别的渠道", tenant_id=1),
            ]
        )
        db.commit()

        everything = build_webridge_dashboard(db, channel, tab="messages")
        assert len(everything["rows"]) == 2
        by_room = build_webridge_dashboard(db, channel, tab="messages", conversation_id=10)
        assert [m.content for m in by_room["rows"]] == ["群消息"]
        by_sender = build_webridge_dashboard(db, channel, tab="messages", sender="老王")
        assert [m.content for m in by_sender["rows"]] == ["私聊"]
        # 其他渠道的会话 ID 不能被当作筛选条件读出数据。
        assert build_webridge_dashboard(db, channel, tab="messages", conversation_id=12)["rows"] == []
        assert {c["id"] for c in everything["conversations"]} == {10, 11}


def test_poll_maintenance_is_throttled_and_refreshes_last_seen() -> None:
    """超时回收和在线心跳按渠道节流：间隔内重复 poll 不再重复执行，首次执行会刷新在线时间。"""

    from unittest.mock import MagicMock, patch

    service._last_poll_maintenance.clear()
    identity = {"device_id": "d1", "session_id": "s1", "account_id": "a1", "last_seen_at": "old", "online": False}
    channel = Channel(id=9, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge", runtime_info_json=json.dumps({"webridge": identity}))
    with patch.object(service, "reap_stale_commands") as reap:
        service._poll_maintenance(MagicMock(), channel)
        service._poll_maintenance(MagicMock(), channel)
    assert reap.call_count == 1
    bridge = json.loads(channel.runtime_info_json)["webridge"]
    assert bridge["online"] is True and bridge["last_seen_at"] != "old"


def test_poll_holds_until_deadline_when_no_commands() -> None:
    """无命令时 poll 必须挂起到截止时间再返回空列表，不能立即返回造成手机紧循环重试。"""

    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.api import wechat_webridge as api

    calls = []

    def fake_claim(channel_id, tenant_id, body):
        calls.append(1)
        return []

    request = AsyncMock()
    request.json.return_value = {}
    request.is_disconnected.return_value = False
    with (
        patch.object(api, "_claim_once", fake_claim),
        patch.object(api, "_channel_tenant", return_value=(1, None)),
        patch.object(api, "_json", AsyncMock(return_value={})),
        patch.object(api, "POLL_HOLD_SECONDS", 0.35),
        patch.object(api, "POLL_CHECK_INTERVAL_SECONDS", 0.1),
    ):
        result, elapsed = asyncio.run(_timed(api.poll(2, request)))
    assert result == {"commands": []}
    assert elapsed >= 0.3
    assert 2 <= len(calls) <= 6


async def _timed(coro):
    """返回协程结果和耗时，供长轮询挂起时长断言使用。"""

    import time

    begin = time.monotonic()
    result = await coro
    return result, time.monotonic() - begin
