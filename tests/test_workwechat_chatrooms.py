from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.main import build_channel_chatroom_summary
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, Message, Plugin, TaskJob
from app.services.workwechat_chatrooms import (
    build_workwechat_chatroom_detail,
    build_workwechat_chatroom_list,
    sync_workwechat_chatroom_from_message,
    sync_workwechat_chatroom_members,
    sync_workwechat_chatrooms,
)


class WorkWechatChatroomTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        self.db = TestingSession()

        # 群聊同步测试只需要机器人主键，名称即为当前唯一的租户可见标识。
        bot = Bot(name="Bot")
        plugin = Plugin(code="weather", display_name="天气插件")
        self.db.add_all([bot, plugin])
        self.db.flush()

        channel = Channel(
            bot_id=bot.id,
            name="企微",
            channel_type="workwechat",
            api_base="http://127.0.0.1:8181",
            adapter_config_json=json.dumps({"headers": {"X-Provider-Token": "provider-token"}}, ensure_ascii=False),
        )
        self.db.add(channel)
        self.db.flush()

        binding = BotPlugin(bot_id=bot.id, plugin_id=plugin.id, status="enabled")
        self.db.add(binding)
        self.db.commit()

        self.bot = bot
        self.plugin = plugin
        self.channel = channel
        self.binding = binding

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_message_ingest_creates_chatroom_member_and_message_flow(self):
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="workwechat",
            external_id="R:room-001",
            title="企微测试群",
            chat_type="group",
        )
        self.db.add(conversation)
        self.db.flush()

        result = sync_workwechat_chatroom_from_message(
            self.db,
            self.channel,
            {
                "event_key": "evt-001",
                "conversation": {"external_id": "R:room-001", "title": "企微测试群", "chat_type": "group"},
                "message": {"sender_id": "user-001", "sender_name": "张三", "message_type": "text", "content": "hello"},
                "details": {"timestamp": "2026-06-05T02:00:00+00:00"},
            },
            conversation=conversation,
        )
        self.db.add(
            Message(
                conversation_id=conversation.id,
                direction="in",
                sender_id="user-001",
                sender_name="张三",
                content="hello",
                message_type="text",
                status="received",
            )
        )
        self.db.commit()

        self.assertTrue(result["chatroom_created"])
        self.assertTrue(result["member_created"])
        chatroom = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:room-001").one()
        member = self.db.query(ChatroomMember).filter_by(chatroom_id=chatroom.id, member_external_id="user-001").one()
        self.assertEqual(chatroom.name, "企微测试群")
        self.assertEqual(chatroom.management_status, "enabled")
        self.assertTrue(chatroom.is_active)
        self.assertEqual(member.nickname, "张三")

        detail = build_workwechat_chatroom_detail(self.db, self.channel, chatroom)
        self.assertEqual(detail["member_record_count"], 1)
        self.assertEqual(detail["message_flow"]["total"], 1)
        self.assertEqual(detail["message_flow"]["items"][0]["content"], "hello")

    def test_new_message_without_title_refreshes_chatroom_name_from_provider_list(self):
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="workwechat",
            external_id="R:auto-profile-room",
            title="R:auto-profile-room",
            chat_type="group",
        )
        self.db.add(conversation)
        self.db.flush()

        def fake_post_json(_self, url, body, *, headers, timeout):
            self.assertTrue(url.endswith("/chatrooms/list"))
            return {
                "ok": True,
                "rooms": [
                    {
                        "conversation_id": "R:auto-profile-room",
                        "nickname": "自动补资料群",
                        "create_user_id": "owner-auto",
                        "total": 9,
                    },
                    {
                        "conversation_id": "R:other-room",
                        "nickname": "不应自动创建的群",
                        "total": 3,
                    },
                ],
            }

        with patch("app.services.channel_adapters.generic.GenericHttpAdapter._post_json", fake_post_json):
            result = sync_workwechat_chatroom_from_message(
                self.db,
                self.channel,
                {
                    "event_key": "evt-auto-profile",
                    "conversation": {"external_id": "R:auto-profile-room", "title": "", "chat_type": "group"},
                    "message": {"sender_id": "user-auto", "sender_name": "张三", "message_type": "text", "content": "hello"},
                    "details": {"timestamp": "2026-06-05T02:00:00+00:00"},
                },
                conversation=conversation,
            )
            self.db.commit()

        self.assertEqual(result["profile_sync_status"], "updated")
        chatroom = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:auto-profile-room").one()
        self.assertEqual(chatroom.name, "自动补资料群")
        self.assertEqual(chatroom.member_count, 9)
        self.assertEqual(conversation.title, "自动补资料群")
        self.assertEqual(self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:other-room").count(), 0)

    def test_cloud_new_message_without_title_enqueues_targeted_profile_sync_once(self):
        self.channel.adapter_config_json = json.dumps({"workwechat_delivery_mode": "cloud_poll"}, ensure_ascii=False)
        self.db.commit()
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="workwechat",
            external_id="R:cloud-profile-room",
            title="R:cloud-profile-room",
            chat_type="group",
        )
        self.db.add(conversation)
        self.db.flush()

        first = sync_workwechat_chatroom_from_message(
            self.db,
            self.channel,
            {
                "event_key": "evt-cloud-profile-1",
                "conversation": {"external_id": "R:cloud-profile-room", "title": "", "chat_type": "group"},
                "message": {"sender_id": "user-cloud", "sender_name": "李四", "message_type": "text", "content": "hello"},
                "details": {"timestamp": "2026-06-05T02:00:00+00:00"},
            },
            conversation=conversation,
        )
        self.db.commit()

        self.assertEqual(first["profile_sync_status"], "queued")
        jobs = self.db.query(TaskJob).filter_by(job_type="workwechat_cloud_control", related_type="chatroom").all()
        self.assertEqual(len(jobs), 1)
        payload = json.loads(jobs[0].payload_json)
        self.assertEqual(payload["action"], "sync_chatrooms")
        self.assertEqual(payload["target_external_id"], "R:cloud-profile-room")

        second = sync_workwechat_chatroom_from_message(
            self.db,
            self.channel,
            {
                "event_key": "evt-cloud-profile-2",
                "conversation": {"external_id": "R:cloud-profile-room", "title": "", "chat_type": "group"},
                "message": {"sender_id": "user-cloud", "sender_name": "李四", "message_type": "text", "content": "hello again"},
                "details": {"timestamp": "2026-06-05T02:01:00+00:00"},
            },
            conversation=conversation,
        )
        self.db.commit()

        self.assertEqual(second["profile_sync_status"], "skipped")
        self.assertEqual(self.db.query(TaskJob).filter_by(job_type="workwechat_cloud_control", related_type="chatroom").count(), 1)

    def test_provider_room_and_member_sync_updates_local_snapshot(self):
        def fake_post_json(_self, url, body, *, headers, timeout):
            self.assertIn("X-Provider-Token", headers)
            if url.endswith("/chatrooms/list"):
                return {
                    "ok": True,
                    "rooms": [
                        {
                            "conversation_id": "R:provider-room",
                            "nickname": "Hook 群",
                            "create_user_id": "owner-001",
                            "total": 2,
                        }
                    ],
                }
            if url.endswith("/chatrooms/members"):
                self.assertEqual(body["conversation_id"], "R:provider-room")
                return {
                    "ok": True,
                    "members": [
                        {
                            "user_id": "member-001",
                            "acctid": "acct-001",
                            "username": "甲",
                            "room_nickname": "甲群名",
                            "is_admin": 1,
                            "small_head_img_url": "https://img.example/member-001-small.jpg",
                            "corp_name": "测试公司",
                            "corp_id": "corp-001",
                            "mobile": "13800138000",
                            "email": "member001@example.com",
                            "position": "运营",
                            "join_time": "1711111111",
                        },
                        {"user_id": "member-002", "realname": "乙", "is_admin": 0},
                    ],
                }
            return {"ok": False, "error": "unexpected_url"}

        with patch("app.services.channel_adapters.generic.GenericHttpAdapter._post_json", fake_post_json):
            overview = sync_workwechat_chatrooms(self.db, self.channel)
            self.db.commit()

            self.assertIn("已从企微 Hook 同步 1 个群", overview["last_status_summary"])
            chatroom = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:provider-room").one()
            self.assertEqual(chatroom.name, "Hook 群")
            member_overview = sync_workwechat_chatroom_members(self.db, self.channel, chatroom)
            self.db.commit()

        self.assertTrue(member_overview["ok"])
        self.assertEqual(member_overview["synced_members"], 2)
        detail = build_workwechat_chatroom_detail(self.db, self.channel, chatroom)
        self.assertEqual(detail["member_record_count"], 2)
        self.assertEqual(detail["role_counts"]["管理员"], 1)
        first_member = next(item for item in detail["members"] if item["member_external_id"] == "member-001")
        self.assertTrue(first_member["has_avatar"])
        self.assertEqual(first_member["avatar_url"], "https://img.example/member-001-small.jpg")
        self.assertEqual(first_member["alias"], "acct-001")
        self.assertIn("测试公司", first_member["region_label"])
        self.assertEqual(first_member["mobile"], "13800138000")
        self.assertEqual(first_member["email"], "member001@example.com")
        self.assertEqual(first_member["join_time"], "1711111111")

        list_view = build_workwechat_chatroom_list(self.db, self.channel)
        self.assertEqual(list_view["items"][0]["member_record_count"], 2)

        summary = build_channel_chatroom_summary(list_view)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["member_snapshot_chatroom_count"], 1)
        self.assertEqual(summary["recent_chatrooms"][0]["external_id"], "R:provider-room")
        self.assertEqual(summary["sync_action"], f"/channels/{self.channel.id}/workwechat/sync-chatrooms")

    def test_message_ingest_does_not_downgrade_synced_chatroom_name(self):
        chatroom = Chatroom(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            external_id="R:synced-room",
            name="已同步供应商群",
            status="profile_synced",
        )
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="workwechat",
            external_id="R:synced-room",
            title="R:synced-room",
            chat_type="group",
        )
        self.db.add_all([chatroom, conversation])
        self.db.commit()

        sync_workwechat_chatroom_from_message(
            self.db,
            self.channel,
            {
                "event_key": "evt-no-title",
                "conversation": {"external_id": "R:synced-room", "title": "", "chat_type": "group"},
                "message": {"sender_id": "user-002", "sender_name": "李四", "message_type": "text", "content": "hello"},
                "details": {"timestamp": "2026-06-05T03:00:00+00:00"},
            },
            conversation=conversation,
        )
        self.db.commit()

        refreshed = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:synced-room").one()
        self.assertEqual(refreshed.name, "已同步供应商群")

    def test_message_ingest_keeps_existing_disabled_chatroom_inactive(self):
        chatroom = Chatroom(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            external_id="R:disabled-room",
            name="已停用群",
            status="profile_synced",
            management_status="disabled",
            is_active=False,
        )
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="workwechat",
            external_id="R:disabled-room",
            title="已停用群",
            chat_type="group",
        )
        self.db.add_all([chatroom, conversation])
        self.db.commit()

        sync_workwechat_chatroom_from_message(
            self.db,
            self.channel,
            {
                "event_key": "evt-disabled",
                "conversation": {"external_id": "R:disabled-room", "title": "已停用群", "chat_type": "group"},
                "message": {"sender_id": "user-003", "sender_name": "王五", "message_type": "text", "content": "hello"},
                "details": {"timestamp": "2026-06-05T04:00:00+00:00"},
            },
            conversation=conversation,
        )
        self.db.commit()

        refreshed = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:disabled-room").one()
        self.assertEqual(refreshed.management_status, "disabled")
        self.assertFalse(refreshed.is_active)


if __name__ == "__main__":
    unittest.main()
