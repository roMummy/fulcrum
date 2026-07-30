from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, WorkWechatContact, WorkWechatContactRelation
from app.services.workwechat_contacts import (
    build_workwechat_contact_detail,
    build_workwechat_contact_list,
    build_workwechat_contact_summary,
    apply_workwechat_contact_event,
    mark_workwechat_contact_deleted,
    restore_workwechat_contact,
    sync_workwechat_contacts,
    upsert_workwechat_contact,
)


class WorkWechatContactTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        self.db = TestingSession()

        # 联系人归属通过渠道和机器人主键确定，测试夹具同步删除旧 slug 参数。
        bot = Bot(name="Bot")
        self.db.add(bot)
        self.db.flush()
        channel = Channel(bot_id=bot.id, name="企微", channel_type="workwechat")
        self.db.add(channel)
        self.db.commit()
        self.bot = bot
        self.channel = channel

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_contact_upsert_is_channel_level_and_not_group_member(self):
        chatroom = Chatroom(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            external_id="R:room-001",
            name="客户群",
        )
        self.db.add(chatroom)
        self.db.flush()
        self.db.add(
            ChatroomMember(
                chatroom_id=chatroom.id,
                bot_id=self.bot.id,
                channel_id=self.channel.id,
                member_external_id="member-001",
                nickname="群昵称",
            )
        )

        contact = upsert_workwechat_contact(
            self.db,
            self.channel,
            {
                "external_userid": "wm-external-001",
                "name": "客户张三",
                "corp_name": "测试公司",
                "mobile": "13800138000",
                "email": "zhangsan@example.com",
                "type": 1,
                "gender": 1,
                "follow_user": [
                    {
                        "userid": "staff-001",
                        "name": "运营一号",
                        "remark": "重点客户",
                        "tags": [{"tag_name": "VIP"}],
                        "createtime": "1711111111",
                    }
                ],
            },
        )
        self.db.commit()

        self.assertEqual(contact.contact_kind, "external_contact")
        self.assertEqual(contact.contact_external_id, "wm-external-001")
        self.assertEqual(self.db.query(ChatroomMember).count(), 1)
        self.assertEqual(self.db.query(WorkWechatContact).count(), 1)
        self.assertEqual(self.db.query(WorkWechatContactRelation).count(), 1)

        list_view = build_workwechat_contact_list(self.db, self.channel)
        self.assertEqual(list_view["total"], 1)
        self.assertEqual(list_view["items"][0]["name"], "客户张三")
        self.assertEqual(list_view["items"][0]["corp_name"], "测试公司")

        detail = build_workwechat_contact_detail(self.db, self.channel, contact)
        self.assertEqual(detail["relations"][0]["owner_userid"], "staff-001")
        self.assertEqual(detail["relations"][0]["remark"], "重点客户")

    def test_mark_deleted_and_restore_keep_contact_row(self):
        contact = upsert_workwechat_contact(
            self.db,
            self.channel,
            {
                "external_userid": "wm-external-002",
                "name": "客户李四",
                "follow_user": [{"userid": "staff-001", "name": "运营一号"}],
            },
        )
        self.db.commit()
        contact_id = contact.id

        mark_workwechat_contact_deleted(
            self.db,
            contact,
            reason="deleted_by_contact",
            event_key="evt-delete-001",
        )
        self.db.commit()

        deleted = self.db.get(WorkWechatContact, contact_id)
        self.assertIsNotNone(deleted)
        self.assertEqual(deleted.sync_status, "deleted")
        self.assertEqual(deleted.delete_reason, "deleted_by_contact")
        self.assertEqual(deleted.relations[0].relationship_status, "deleted_by_contact")
        self.assertTrue(deleted.relations[0].is_deleted)

        summary = build_workwechat_contact_summary(self.db, self.channel)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["deleted"], 1)

        restore_workwechat_contact(self.db, deleted)
        self.db.commit()

        restored = self.db.get(WorkWechatContact, contact_id)
        self.assertEqual(restored.sync_status, "active")
        self.assertIsNone(restored.deleted_at)
        self.assertEqual(restored.relations[0].relationship_status, "active")
        self.assertFalse(restored.relations[0].is_deleted)

    def test_contact_list_filters_by_query_status_and_kind(self):
        upsert_workwechat_contact(
            self.db,
            self.channel,
            {"external_userid": "wm-a", "name": "外部客户", "corp_name": "甲公司"},
        )
        internal = upsert_workwechat_contact(
            self.db,
            self.channel,
            {"userid": "staff-a", "name": "内部同事", "source": "provider_sync"},
        )
        mark_workwechat_contact_deleted(self.db, internal, reason="sync_missing")
        self.db.commit()

        active_list = build_workwechat_contact_list(self.db, self.channel, sync_status="active")
        self.assertEqual(active_list["total"], 1)
        self.assertEqual(active_list["items"][0]["contact_external_id"], "wm-a")

        internal_list = build_workwechat_contact_list(self.db, self.channel, contact_kind="internal_member")
        self.assertEqual(internal_list["total"], 1)
        self.assertEqual(internal_list["items"][0]["contact_external_id"], "staff-a")

        query_list = build_workwechat_contact_list(self.db, self.channel, query="甲公司")
        self.assertEqual(query_list["total"], 1)
        self.assertEqual(query_list["items"][0]["name"], "外部客户")

    def test_sync_workwechat_contacts_reads_provider_contact_lists(self):
        self.channel.api_base = "http://127.0.0.1:8181"
        self.db.commit()

        def fake_post_json(_self, url, body, *, headers, timeout):
            self.assertTrue(url.endswith("/contacts/list"))
            self.assertEqual(body["contact_kind"], "all")
            return {
                "ok": True,
                "contacts": [
                    {
                        "contact_kind": "internal_member",
                        "user_id": "staff-001",
                        "username": "内部张三",
                        "mobile": "13800138000",
                        "corp_id": "corp-001",
                    },
                    {
                        "contact_kind": "external_contact",
                        "user_id": "external-001",
                        "username": "外部李四",
                        "avatar": "https://img.example/avatar.jpg",
                        "corp_id": "corp-002",
                    },
                ],
            }

        with patch("app.services.channel_adapters.generic.GenericHttpAdapter._post_json", fake_post_json):
            result = sync_workwechat_contacts(self.db, self.channel)
            self.db.commit()

        self.assertTrue(result["ok"])
        self.assertEqual(result["synced_contacts"], 2)
        contacts = self.db.query(WorkWechatContact).order_by(WorkWechatContact.contact_external_id.asc()).all()
        self.assertEqual([item.contact_external_id for item in contacts], ["external-001", "staff-001"])
        self.assertEqual(contacts[0].contact_kind, "external_contact")
        self.assertEqual(contacts[1].contact_kind, "internal_member")

    def test_contact_deleted_hook_marks_contact_deleted(self):
        result = apply_workwechat_contact_event(
            self.db,
            self.channel,
            {
                "event_key": "evt-delete-hook",
                "event_type": "contact_deleted",
                "details": {
                    "hook_type": 11077,
                    "raw_data": {
                        "user_id": "external-002",
                        "username": "被删客户",
                        "avatar": "https://img.example/deleted.jpg",
                        "corp_id": "corp-003",
                    },
                },
            },
        )
        self.db.commit()

        self.assertEqual(result["action"], "deleted")
        contact = self.db.query(WorkWechatContact).filter_by(contact_external_id="external-002").one()
        self.assertEqual(contact.sync_status, "deleted")
        self.assertEqual(contact.delete_reason, "deleted_by_contact")
        self.assertEqual(contact.delete_event_key, "evt-delete-hook")


if __name__ == "__main__":
    unittest.main()
