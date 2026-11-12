"""咕嘎助手 Agent 设备注册与命令队列测试。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import open_tenant_session
from app.models.entities import Channel, Message, WorkWechatContact
from app.services.guga_agent import service
from app.services.guga_agent.contract import COMMAND_NAMES, GugaAgentError


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'guga_agent.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _channel(db, *, enabled: bool = True) -> Channel:
    channel = Channel(
        bot_id=1,
        name="咕嘎助手",
        channel_type="wechat_gugatool",
        is_enabled=enabled,
        tenant_id=7,
        runtime_info_json=json.dumps({"account_connection": {"account_id": "wxid_self", "state": "verified"}}),
    )
    db.add(channel)
    db.flush()
    return channel


def test_register_is_idempotent_and_blocks_identity_switch(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        first = service.register_device(
            db,
            channel,
            {
                "device_id": "device-1",
                "account_id": "wxid_self",
                "profile": {"wxid": "wxid_self", "nickname": "咕咕嘎嘎", "avatar_url": "https://example.com/a.jpg"},
            },
        )
        second = service.register_device(db, channel, {"device_id": "device-1", "account_id": "wxid_self"})

        assert first["session_id"] == second["session_id"]
        runtime = json.loads(channel.runtime_info_json)
        assert runtime["account_profile"]["name"] == "咕咕嘎嘎"
        assert runtime["account_profile"]["avatar_url"] == "https://example.com/a.jpg"

        with pytest.raises(GugaAgentError) as exc:
            service.register_device(db, channel, {"device_id": "device-2", "account_id": "wxid_self"})
        assert exc.value.code == "identity_mismatch"
        assert exc.value.status == 409


def test_poll_claims_command_and_rejects_expired_session(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        registered = service.register_device(db, channel, {"device_id": "device-1", "account_id": "wxid_self"})
        job = service.enqueue_command(
            db,
            channel,
            command_name="send_text",
            params={"to_wxid": "wxid_peer", "content": "hi"},
            request_id="req-1",
        )
        db.commit()

        commands = service.claim_commands(
            db,
            channel,
            {
                "device_id": "device-1",
                "account_id": "wxid_self",
                "session_id": registered["session_id"],
            },
        )

        assert {
            "request_id": "req-1",
            "command_name": "send_text",
            "params": {"to_wxid": "wxid_peer", "content": "hi"},
        } in commands
        assert job.status == "running"

        # 手机端轮询只带 device_id + session_id，不带 account_id。
        service.enqueue_command(
            db,
            channel,
            command_name="send_text",
            params={"to_wxid": "wxid_peer", "content": "again"},
            request_id="req-2",
        )
        db.commit()
        commands = service.claim_commands(
            db,
            channel,
            {"device_id": "device-1", "session_id": registered["session_id"]},
        )
        assert commands[0]["request_id"] == "req-2"

        with pytest.raises(GugaAgentError) as exc:
            service.claim_commands(
                db,
                channel,
                {"device_id": "device-1", "account_id": "wxid_self", "session_id": "expired"},
            )
        assert exc.value.code == "session_expired"
        assert exc.value.status == 410


def test_command_result_is_idempotent(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        service.register_device(db, channel, {"device_id": "device-1", "account_id": "wxid_self"})
        job = service.enqueue_command(
            db,
            channel,
            command_name="send_text",
            params={"to_wxid": "wxid_peer", "content": "hi"},
            request_id="req-2",
        )
        db.commit()

        first = service.accept_command_result(
            db,
            channel,
            {"device_id": "device-1", "request_id": "req-2", "command_name": "send_text", "ok": True, "result": {"Data": {}}},
        )
        second = service.accept_command_result(
            db,
            channel,
            {"device_id": "device-1", "request_id": "req-2", "command_name": "send_text", "ok": True, "result": {}},
        )
        unknown = service.accept_command_result(
            db,
            channel,
            {"device_id": "device-1", "request_id": "missing", "command_name": "send_text", "ok": True},
        )

        assert first["accepted"] is True and first["deduplicated"] is False
        assert second["deduplicated"] is True
        assert unknown == {"ok": True, "accepted": False, "deduplicated": False}
        assert job.status == "completed"


def test_enqueue_rejects_unknown_command(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)

        with pytest.raises(GugaAgentError) as exc:
            service.enqueue_command(db, channel, command_name="unknown_command", params={})
        assert exc.value.code == "unsupported_guga_command"


def test_friend_list_result_persists_friend_contacts(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        service.register_device(db, channel, {"device_id": "device-1", "account_id": "wxid_self"})
        service.enqueue_command(db, channel, command_name="get_friend_list", params={}, request_id="friends-1")
        db.commit()

        result = service.accept_command_result(
            db,
            channel,
            {
                "device_id": "device-1",
                "request_id": "friends-1",
                "command_name": "get_friend_list",
                "ok": True,
                "result": {
                    "Data": {
                        "Count": 3,
                        "Items": [
                            {"Wxid": "wxid_friend_a", "Name": "好友A", "NickName": "昵称A", "Remark": "备注A", "Alias": "a"},
                            {"Wxid": "wxid_friend_b", "Name": "好友B", "NickName": "", "Remark": "", "Alias": ""},
                            {"Wxid": "123@chatroom", "Name": "保存的群", "NickName": "", "Remark": "", "Alias": ""},
                        ],
                    }
                },
            },
        )

        assert result["status"] == "completed"
        contacts = db.scalars(select(WorkWechatContact).order_by(WorkWechatContact.id.asc())).all()
        assert [item.contact_external_id for item in contacts] == ["wxid_friend_a", "wxid_friend_b"]
        assert all(item.contact_kind == "friend" for item in contacts)
        assert contacts[0].display_name == "备注A"
        assert contacts[0].alias == "a"
        assert contacts[1].name == "好友B"


def test_all_phone_capabilities_are_registered_as_commands() -> None:
    """手机端执行器支持的接口必须在平台白名单里有对应命令。"""

    expected = {
        "send_mini_msg",
        "send_tail",
        "revoke_message",
        "delete_session",
        "get_profile",
        "get_health",
        "get_recent_messages",
        "sync_messages",
        "get_friend_detail",
        "get_system_logs",
        "get_webhook_config",
        "update_webhook_config",
        "get_mini_cloud_classes",
        "get_mini_cloud_hooks",
        "get_mini_cloud_recent",
        "install_mini_cloud_hook",
        "invoke_mini_cloud",
        "clear_mini_cloud_records",
    }
    assert expected <= COMMAND_NAMES


def test_friend_detail_updates_contact_and_drops_loopback_avatar(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        service.register_device(db, channel, {"device_id": "device-1", "account_id": "wxid_self"})
        service.enqueue_command(db, channel, command_name="get_friend_detail", params={"Towxids": "wxid_friend_a"}, request_id="detail-1")
        db.commit()

        service.accept_command_result(
            db,
            channel,
            {
                "device_id": "device-1",
                "request_id": "detail-1",
                "command_name": "get_friend_detail",
                "ok": True,
                "result": {
                    "Data": {
                        "Count": 1,
                        "Items": [
                            {
                                "Wxid": "wxid_friend_a",
                                "Name": "好友A",
                                "NickName": "昵称A",
                                "Remark": "备注A",
                                "Alias": "a",
                                "AvatarDownloadURL": "http://127.0.0.1:19090/api/v1/Tools/DownloadFile?name=a.jpg",
                            }
                        ],
                    }
                },
            },
        )

        contact = db.scalar(select(WorkWechatContact).where(WorkWechatContact.contact_external_id == "wxid_friend_a"))
        assert contact is not None
        assert contact.contact_kind == "friend"
        assert contact.display_name == "备注A"
        # 回环地址服务端访问不到，不应写入头像。
        assert contact.avatar_url is None


def test_profile_result_updates_account_profile(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        service.register_device(db, channel, {"device_id": "device-1", "account_id": "wxid_self"})
        service.enqueue_command(db, channel, command_name="get_profile", params={}, request_id="profile-1")
        db.commit()

        service.accept_command_result(
            db,
            channel,
            {
                "device_id": "device-1",
                "request_id": "profile-1",
                "command_name": "get_profile",
                "ok": True,
                "result": {
                    "Data": {
                        "Wxid": "wxid_self",
                        "NickName": "咕咕嘎嘎",
                        "HeadImgUrl": "https://example.com/self.jpg",
                    }
                },
            },
        )

        runtime = json.loads(channel.runtime_info_json)
        assert runtime["account_profile"]["name"] == "咕咕嘎嘎"
        assert runtime["account_profile"]["avatar_url"] == "https://example.com/self.jpg"


def test_sync_messages_result_ingests_history(session_factory) -> None:
    with tenant_scope(7), open_tenant_session(session_factory) as db:
        channel = _channel(db)
        service.register_device(db, channel, {"device_id": "device-1", "account_id": "wxid_self"})
        service.enqueue_command(db, channel, command_name="sync_messages", params={"Limit": 10}, request_id="sync-1")
        db.commit()

        service.accept_command_result(
            db,
            channel,
            {
                "device_id": "device-1",
                "request_id": "sync-1",
                "command_name": "sync_messages",
                "ok": True,
                "result": {
                    "Data": {
                        "Count": 1,
                        "Items": [
                            {
                                "id": "hist-1",
                                "kind": "incoming",
                                "fromUser": "wxid_peer",
                                "toUser": "wxid_self",
                                "messageType": 1,
                                "content": "历史消息",
                                "createTime": 1700000000,
                                "isHistory": True,
                                "localMessageID": 1,
                                "serverMessageID": 2,
                            }
                        ],
                    }
                },
            },
        )

        messages = db.scalars(select(Message)).all()
        assert [item.external_message_id for item in messages] == ["hist-1"]
