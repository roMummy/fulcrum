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
    assert captured[0]["payload"]["command"]["params"]["image_path"].endswith("a.jpg")
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

    def scalars(self, *_args, **_kwargs):
        return SimpleNamespace(all=lambda: [])

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
        {"items": [{"account_id": "10000000001@chatroom", "nick_name": "示例群", "avatar_url": "https://example.test/a.png"}]},
    )
    assert counts["groups"] == 1
    room = db.added[0]
    assert room.external_id == "10000000001@chatroom"
    assert room.name == "示例群"

    db = _FakeDirectoryDB()
    service.persist_directory_result(
        db, channel, {"type": "friends_all"}, {"items": [{"account_id": "wxid_a", "nick_name": "小柒", "remark": "", "display_name": ""}]}
    )
    assert db.added[0].name == "小柒"
    assert db.added[0].display_name == "小柒"


def test_groups_sync_enables_new_rooms_but_keeps_disabled_rooms_disabled() -> None:
    """同步群列表:新群默认启用;已存在的群保持后台设置,手动停用的群不能被同步重新打开。"""

    from app.models.entities import Chatroom

    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge")
    item = {"items": [{"account_id": "g1@chatroom", "nick_name": "新群"}]}
    db = _FakeDirectoryDB()
    service.persist_directory_result(db, channel, {"type": "groups_all"}, item)
    assert db.added[0].is_active is True and db.added[0].management_status == "enabled"

    class _ExistingDB(_FakeDirectoryDB):
        def __init__(self, room) -> None:
            super().__init__()
            self.room = room

        def scalar(self, *_args, **_kwargs):
            return self.room

    disabled = Chatroom(channel_id=2, external_id="g1@chatroom", name="旧名", is_active=False, management_status="disabled")
    service.persist_directory_result(_ExistingDB(disabled), channel, {"type": "groups_all"}, item)
    assert disabled.is_active is False and disabled.management_status == "disabled"
    assert disabled.name == "新群"  # 资料照常更新,只是不改启用状态


def test_room_members_sync_marks_missing_members_left_and_keeps_them() -> None:
    """同步群成员:名单里没有的老群友保留并标记已退群;回群的恢复在群;不完整名单不动。"""

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.core.tenant_context import tenant_scope
    from app.core.tenant_db import tenant_session
    from app.models.entities import Chatroom, ChatroomMember

    engine = create_engine("sqlite://")
    for table in (Chatroom, ChatroomMember):
        table.__table__.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge")
    command = {"type": "room_members", "params": {"room_id": "g@chatroom"}}

    def members(*ids: str) -> dict:
        return {"members": [{"account_id": i, "nick_name": f"昵称{i}"} for i in ids]}

    with tenant_scope(1):
        with tenant_session(factory) as db:
            room = Chatroom(tenant_id=1, bot_id=1, channel_id=2, external_id="g@chatroom", name="群", member_count=3)
            db.add(room)
            db.flush()
            for ext, status in (("a", "active"), ("b", "active"), ("c", "left")):
                db.add(ChatroomMember(tenant_id=1, chatroom_id=room.id, bot_id=1, channel_id=2, member_external_id=ext, nickname=ext, status=status))
            db.flush()
            status_of = lambda: {m.member_external_id: m.status for m in db.scalars(select(ChatroomMember))}

            # 名单只剩 a、c:b 被标记已退群(仍在表里),c 回群恢复在群
            counts = service.persist_directory_result(db, channel, command, members("a", "c"))
            assert status_of() == {"a": "active", "b": "left", "c": "active"}
            assert counts["members_left"] == 1 and room.member_count == 2

            # 空名单、被截断的名单都不是完整名单:不做任何标记
            service.persist_directory_result(db, channel, command, {"members": []})
            service.persist_directory_result(db, channel, command, {**members("a"), "truncated": True})
            assert status_of() == {"a": "active", "b": "left", "c": "active"}


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

    from app.models.entities import WebridgeDeviceSample

    service._last_poll_maintenance.clear()
    engine = create_engine(f"sqlite:///{tmp_path / 'claim.db'}")
    TaskJob.__table__.create(engine)
    WebridgeDeviceSample.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(TaskJob(
        tenant_id=1, job_type=service.JOB_TYPE, status="pending", priority=100,
        payload_json=json.dumps({"channel_id": 2, "payload": {"message_type": "text", "receiver_id": "r@chatroom", "content": "你好"}}),
    ))
    db.commit()
    identity = {"device_id": "d1", "session_id": "s1", "account_id": "a1"}
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge",
                      runtime_info_json=json.dumps({"webridge": identity}))

    # 维护会入队 device_status / account_status / 群列表对账，裸会话没有租户补全；这里只验证领取逻辑，故桩掉。
    from unittest.mock import patch

    from app.services.wechat_webridge import group_sync, member_directory

    with patch.object(service, "_enqueue_device_status"), patch.object(service, "_enqueue_account_status"), patch.object(group_sync, "reconcile_if_due"), patch.object(member_directory, "retry_due"):
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


def test_record_device_sample_counts_backlog_and_upserts_per_minute(tmp_path) -> None:
    """采样统计本渠道积压命令，不含其他渠道；同一分钟重复采样只更新一行，state 数值被带入。"""

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.models.entities import TaskJob, WebridgeDeviceSample

    engine = create_engine(f"sqlite:///{tmp_path / 'sample.db'}")
    TaskJob.__table__.create(engine)
    WebridgeDeviceSample.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    for channel_id, status in ((2, "pending"), (2, "running"), (2, "pending"), (3, "pending")):
        db.add(TaskJob(tenant_id=1, job_type=service.JOB_TYPE, status=status, priority=1, payload_json=json.dumps({"channel_id": channel_id})))
    db.commit()
    runtime = {"webridge": {"state": {"cached_message_count": 200, "pending_event_queue_count": 0, "is_logged_in": True}}}
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge", runtime_info_json=json.dumps(runtime))

    service.record_device_sample(db, channel)
    service.record_device_sample(db, channel)
    db.commit()

    rows = db.scalars(select(WebridgeDeviceSample)).all()
    assert len(rows) == 1
    assert (rows[0].pending_commands, rows[0].running_commands) == (2, 1)
    assert (rows[0].cached_message_count, rows[0].pending_event_queue_count) == (200, 0)


def test_device_panel_maps_state_fields_and_charts(tmp_path) -> None:
    """iPhone 状态区展示全部已上报字段（已知字段中文标签，未知字段进其他上报），并生成可画图的序列。"""

    from datetime import timedelta

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.time import now_shanghai
    from app.models.entities import WebridgeDeviceSample
    from app.services.wechat_webridge.dashboard import build_webridge_device_panel

    engine = create_engine(f"sqlite:///{tmp_path / 'panel.db'}")
    WebridgeDeviceSample.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    base = now_shanghai().replace(second=0, microsecond=0)
    for index, pending in enumerate((0, 3, 1)):
        db.add(WebridgeDeviceSample(tenant_id=1, channel_id=2, bucket_start=base - timedelta(minutes=2 - index), pending_commands=pending, cached_message_count=None))
    db.commit()
    runtime = {"webridge": {"online": True, "runtime_version": "8.0.80-6", "capabilities": ["a", "b"], "state": {"is_logged_in": True, "cached_message_count": 200, "mystery": "x"}}}
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge", runtime_info_json=json.dumps(runtime))

    panel = build_webridge_device_panel(db, channel)

    assert panel["online"] is True and panel["capability_count"] == 2
    assert {"label": "微信登录", "value": "是"} in panel["fields"]
    assert {"label": "mystery", "value": "x"} in panel["extra"]
    assert [item["value"] for item in panel["pending_chart"]["items"]] == [0, 3, 1]
    assert panel["cache_chart"]["items"] == []


def test_device_status_report_saves_snapshot_and_sample(tmp_path) -> None:
    """device_status 回报写入渠道快照与当前分钟采样；读不到的 null 保持为空，页面条目省略采集失败的内存。"""

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.models.entities import TaskJob, WebridgeDeviceSample
    from app.services.wechat_webridge.dashboard import _hardware_items

    engine = create_engine(f"sqlite:///{tmp_path / 'dev.db'}")
    TaskJob.__table__.create(engine)
    WebridgeDeviceSample.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge", runtime_info_json=json.dumps({"webridge": {}}))
    report = {
        "battery": {"percent": 89, "state": "unplugged"},
        "system_memory": {"ok": True, "used_estimate_percent": 69.58, "available_estimate_mb": 604.9, "physical_total_mb": 1988.9},
        "process_memory": {"phys_footprint_mb": 729.0, "resident_mb": 433.5},
        "device": {"device_model": "iPhone8,2", "ios_version": "15.8.8"},
        "thermal": {"state": "nominal"},
        "network": {"connected": True, "network_type": "wifi", "cellular_generation": ""},
        "application": {"state": "background"},
        "power": {"low_power_mode": False},
    }

    service._save_device_status(db, channel, report)
    db.commit()

    sample = db.scalars(select(WebridgeDeviceSample)).one()
    assert (sample.battery_percent, sample.memory_used_percent, sample.process_memory_mb) == (89, 70, 729)
    saved = json.loads(channel.runtime_info_json)["webridge"]["device"]
    values = {item["label"]: item["value"] for item in _hardware_items(saved)}
    assert values["机型"] == "iPhone8,2 · iOS 15.8.8" and values["电量"].startswith("89%") and values["网络"] == "Wi-Fi"
    # 系统内存采集失败时只有 ok:false，不应展示内存行，也不能抛错。
    saved["system_memory"] = {"ok": False, "error": "x"}
    assert "系统内存" not in {item["label"] for item in _hardware_items(saved)}


def test_enqueue_device_status_skips_when_one_is_unfinished() -> None:
    """同渠道已有未完成的 device_status 时不重复入队；其他渠道的不影响。"""

    from unittest.mock import MagicMock, patch

    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge")
    mine = SimpleNamespace(payload_json=json.dumps({"channel_id": 2}))
    other = SimpleNamespace(payload_json=json.dumps({"channel_id": 3}))
    db = MagicMock()
    with patch.object(service, "parse_job_payload", side_effect=lambda job: json.loads(job.payload_json)), patch.object(service, "enqueue_command") as enqueue:
        enqueue.return_value = SimpleNamespace(priority=0)
        db.scalars.return_value.all.return_value = [other, mine]
        service._enqueue_device_status(db, channel)
        enqueue.assert_not_called()
        db.scalars.return_value.all.return_value = [other]
        service._enqueue_device_status(db, channel)
        enqueue.assert_called_once()
        assert enqueue.call_args.kwargs["command_name"] == "device_status"
        assert enqueue.return_value.priority == service.DEVICE_STATUS_PRIORITY


def test_live_summary_backlog_excludes_device_status_and_other_channels(tmp_path) -> None:
    """顶部摘要的积压只统计本渠道业务命令；内部 device_status 采样命令和其他渠道不计入，电量/内存读自快照。"""

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models.entities import TaskJob
    from app.services.wechat_webridge.dashboard import build_webridge_live_summary

    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    TaskJob.__table__.create(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    rows = ((2, "pending", 90), (2, "running", 90), (2, "pending", service.DEVICE_STATUS_PRIORITY), (3, "pending", 90))
    for channel_id, status, priority in rows:
        db.add(TaskJob(tenant_id=1, job_type=service.JOB_TYPE, status=status, priority=priority, payload_json=json.dumps({"channel_id": channel_id})))
    db.commit()
    device = {"battery": {"percent": 88, "state": "charging"}, "system_memory": {"ok": True, "used_estimate_percent": 69.6}}
    channel = Channel(id=2, bot_id=1, tenant_id=1, name="微桥", channel_type="wechat_webridge", runtime_info_json=json.dumps({"webridge": {"online": True, "device": device}}))

    live = build_webridge_live_summary(db, channel)

    assert (live["pending"], live["running"]) == (1, 1)
    assert (live["battery_percent"], live["battery_state"], live["memory_used_percent"]) == (88, "充电中", 70)
    # 系统内存采集失败时不显示内存，避免展示误导数字。
    device["system_memory"] = {"ok": False}
    channel.runtime_info_json = json.dumps({"webridge": {"online": True, "device": device}})
    assert build_webridge_live_summary(db, channel)["memory_used_percent"] is None


def _reply_context(channel_type: str, chat_type: str, config: dict | None = None, message_type: str = "text"):
    """构造回复方式测试用的最小插件上下文。"""

    from app.services.plugin_directory import DirectoryPluginContext

    return DirectoryPluginContext(
        plugin_code="demo", binding_id=1, bot_id=1, channel_id=9, channel_type=channel_type,
        chat_type=chat_type, message_type=message_type, command_prefix="/", content="/签到",
        sender_id="wxid_a", conversation_external_id="1@chatroom", mentioned_self=False,
        binding_config=config or {},
        normalized_event={"message": {"external_message_id": "555"}},
    )


def test_reply_mode_quotes_trigger_message_in_webridge_group() -> None:
    """微桥群聊默认引用触发消息；both 同时 @；私聊和其他渠道保持原样。"""

    from app.services.plugin_directory import _apply_reply_mode

    base = [{"message_type": "text", "content": "ok"}]
    quoted = _apply_reply_mode(_reply_context("wechat_webridge", "group"), {}, [dict(base[0])])
    assert quoted[0]["quote_message_id"] == "555"
    both = _apply_reply_mode(_reply_context("wechat_webridge", "group"), {"reply_mode": "both"}, [dict(base[0])])
    assert both[0]["content"].startswith("{{mention:wxid_a}}") and both[0]["quote_message_id"] == "555"
    assert _apply_reply_mode(_reply_context("wechat_webridge", "direct"), {}, [dict(base[0])]) == base
    assert _apply_reply_mode(_reply_context("telegram", "group"), {}, [dict(base[0])]) == base
    assert _apply_reply_mode(_reply_context("wechat_webridge", "group", {"reply_mode": "plain"}), {}, [dict(base[0])]) == base


def test_reply_mode_skips_quote_for_member_events() -> None:
    """成员加入/退出/改昵称是合成事件,没有可引用的微信消息,引用会让手机端整条拒发。"""

    from app.services.plugin_directory import _apply_reply_mode

    base = [{"message_type": "text", "content": "通知"}]
    for message_type in ("group_member_join", "group_member_leave", "group_member_nickname_change"):
        context = _reply_context("wechat_webridge", "group", message_type=message_type)
        assert _apply_reply_mode(context, {"reply_mode": "both"}, [dict(base[0])]) == base


def test_webridge_text_command_carries_quote_and_mentions(monkeypatch) -> None:
    """文本出站命令必须透传引用 ID 和 @ 名单。"""

    from types import SimpleNamespace
    from app.models.entities import Channel, Conversation
    from app.services.wechat_webridge import service

    captured: list[dict] = []
    monkeypatch.setattr(service, "enqueue_job", lambda db, **kw: captured.append(kw) or SimpleNamespace(id=1))
    channel = Channel(id=9, bot_id=1, tenant_id=3, name="微桥", channel_type="wechat_webridge")
    conversation = Conversation(id=11, bot_id=1, channel_id=9, external_id="1@chatroom", chat_type="group", tenant_id=1)
    # 出站前会查"bot 是否已退出这个群":给一个最小的数据库替身(查不到已退出记录)。
    db = SimpleNamespace(scalar=lambda stmt: None)
    service.enqueue_message_command(db, channel, conversation, {"message_type": "text", "content": "hi", "quote_message_id": "555", "mention_user_list": ["wxid_a"]})
    params = captured[0]["payload"]["command"]["params"]
    assert params["quote_message_id"] == "555" and params["mention_user_list"] == ["wxid_a"]


def test_webridge_is_at_maps_to_mentioned_self() -> None:
    """微桥 is_at 必须映射为 mentioned_self，否则群里 @ 机器人无法触发 require_mention 插件和 AI。"""

    channel = Channel(id=7, bot_id=1, name="微桥", channel_type="wechat_webridge")
    adapter = WechatWeBridgeAdapter()
    at_me = adapter.normalize_inbound_payload(channel, {"body": {"msg_type": "text", "chat_id": "1@chatroom", "is_group": True, "content": "hi", "is_at": True}})
    plain = adapter.normalize_inbound_payload(channel, {"body": {"msg_type": "text", "chat_id": "1@chatroom", "is_group": True, "content": "hi"}})
    assert at_me["details"]["mentioned_self"] is True
    assert plain["details"]["mentioned_self"] is False
