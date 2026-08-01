from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, Channel, Conversation, CustomerOperationRequest, TaskJob, WorkWechatContact
from app.models.saas import Tenant
from app.services.customer_operations import submit_customer_operation
from app.services.task_queue import WORKWECHAT_CLOUD_OUTBOUND_JOB
from app.services.workwechat_contacts import mark_workwechat_contact_deleted, upsert_workwechat_contact


class CustomerOperationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        with TestingSession() as platform_db:
            tenant = Tenant(code="customer-ops-test", name="客户运营测试租户", status="active")
            platform_db.add(tenant)
            platform_db.commit()
            platform_db.refresh(tenant)
            self.tenant_id = tenant.id

        # 服务层写入必须在可信租户上下文内执行，由 before_flush 自动补齐所有
        # 新业务对象的 tenant_id；测试不再逐个伪造字段来绕过真实隔离链路。
        self.tenant_scope_context = tenant_scope(self.tenant_id)
        self.tenant_scope_context.__enter__()
        self.tenant_db_context = tenant_session(TestingSession)
        self.db = self.tenant_db_context.__enter__()

        # 客户运营数据通过机器人主键关联，测试夹具不再携带旧 slug。
        bot = Bot(name="Bot")
        self.db.add(bot)
        self.db.flush()
        channel = Channel(
            bot_id=bot.id,
            name="企微",
            channel_type="workwechat",
            adapter_config_json=json.dumps({"customer_operation_token": "secret-token"}, ensure_ascii=False),
        )
        self.db.add(channel)
        self.db.commit()
        self.bot = bot
        self.channel = channel

    def tearDown(self):
        self.tenant_db_context.__exit__(None, None, None)
        self.tenant_scope_context.__exit__(None, None, None)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _create_contact(self, *, user_id: str = "external-001", conversation_id: str = "S:staff_external") -> WorkWechatContact:
        contact = upsert_workwechat_contact(
            self.db,
            self.channel,
            {
                "contact_kind": "external_contact",
                "user_id": user_id,
                "external_userid": user_id,
                "username": "客户一号",
                "conversation_id": conversation_id,
            },
        )
        self.db.commit()
        return contact

    def test_submit_customer_operation_queues_outbound_job(self):
        contact = self._create_contact()

        result = submit_customer_operation(
            self.db,
            {
                "request_id": "req-001",
                "channel_id": self.channel.id,
                "target": {"external_userid": contact.external_userid},
                "scene": "order_followup",
                "content": {"type": "text", "text": "订单已处理"},
                "biz_payload": {"order_id": "order-001", "source": "crm"},
            },
        )
        self.db.commit()

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "queued")
        operation = self.db.get(CustomerOperationRequest, result["operation_id"])
        self.assertEqual(operation.contact_id, contact.id)
        self.assertEqual(operation.content_type, "text")
        self.assertIsNotNone(operation.job_id)
        conversation = self.db.get(Conversation, operation.conversation_id)
        self.assertEqual(conversation.external_id, "S:staff_external")
        job = self.db.get(TaskJob, operation.job_id)
        payload = json.loads(job.payload_json)
        self.assertEqual(payload["customer_operation_request_id"], operation.id)
        self.assertEqual(payload["payload"]["message_type"], "text")
        self.assertEqual(payload["payload"]["receiver_id"], "S:staff_external")

    def test_submit_customer_operation_uses_cloud_outbound_job_for_cloud_poll_channel(self):
        self.channel.adapter_config_json = json.dumps(
            {
                "customer_operation_token": "secret-token",
                "workwechat_delivery_mode": "cloud_poll",
            },
            ensure_ascii=False,
        )
        contact = self._create_contact()

        result = submit_customer_operation(
            self.db,
            {
                "request_id": "req-cloud",
                "channel_id": self.channel.id,
                "target": {"external_userid": contact.external_userid},
                "content": {"type": "text", "text": "云端 WS 发送"},
            },
        )
        self.db.commit()

        operation = self.db.get(CustomerOperationRequest, result["operation_id"])
        job = self.db.get(TaskJob, operation.job_id)
        self.assertEqual(job.job_type, WORKWECHAT_CLOUD_OUTBOUND_JOB)

    def test_submit_customer_operation_is_idempotent_by_channel_request_id(self):
        contact = self._create_contact()
        payload = {
            "request_id": "req-dup",
            "channel_id": self.channel.id,
            "target": {"external_userid": contact.external_userid},
            "content": {"type": "text", "text": "hello"},
        }

        first = submit_customer_operation(self.db, payload)
        second = submit_customer_operation(self.db, payload)
        self.db.commit()

        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(self.db.query(TaskJob).count(), 1)

    def test_submit_customer_operation_rejects_deleted_contact(self):
        contact = self._create_contact()
        mark_workwechat_contact_deleted(self.db, contact, reason="deleted_by_contact")
        self.db.commit()

        result = submit_customer_operation(
            self.db,
            {
                "request_id": "req-deleted",
                "channel_id": self.channel.id,
                "target": {"external_userid": contact.external_userid},
                "content": {"type": "text", "text": "hello"},
            },
        )
        self.db.commit()

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["status_reason"], "contact_deleted")
        self.assertEqual(self.db.query(TaskJob).count(), 0)

    def test_submit_customer_operation_accepts_link_card_and_mini_program_payloads(self):
        contact = self._create_contact()

        card = submit_customer_operation(
            self.db,
            {
                "request_id": "req-card",
                "channel_id": self.channel.id,
                "target": {"contact_external_id": contact.contact_external_id},
                "content": {
                    "type": "link_card",
                    "url": "https://example.com/order/1",
                    "image_url": "https://example.com/card.jpg",
                    "title": "订单详情",
                    "desc": "查看订单处理进度",
                },
            },
        )
        mini = submit_customer_operation(
            self.db,
            {
                "request_id": "req-mini",
                "channel_id": self.channel.id,
                "target": {"contact_external_id": contact.contact_external_id},
                "content": {
                    "type": "mini_program",
                    "username": "gh_demo@app",
                    "appid": "wx-demo",
                    "appname": "业务小程序",
                    "appicon": "https://example.com/icon.png",
                    "title": "查看订单",
                    "page_path": "pages/order/detail?id=1",
                    "file_id": "file-id",
                    "aes_key": "aes-key",
                    "md5": "md5-value",
                    "size": 123,
                },
            },
        )
        self.db.commit()

        self.assertEqual(card["content_type"], "link_card")
        self.assertEqual(mini["content_type"], "mini_program")
        payloads = [json.loads(job.payload_json)["payload"] for job in self.db.scalars(select(TaskJob).order_by(TaskJob.id.asc())).all()]
        self.assertEqual(payloads[0]["message_type"], "link_card")
        self.assertEqual(payloads[0]["url"], "https://example.com/order/1")
        self.assertEqual(payloads[1]["message_type"], "mini_program")
        self.assertEqual(payloads[1]["page_path"], "pages/order/detail?id=1")

    def test_contact_normalization_stores_conversation_external_id(self):
        contact = self._create_contact(conversation_id="S:staff_customer_002")

        self.assertEqual(contact.conversation_external_id, "S:staff_customer_002")


if __name__ == "__main__":
    unittest.main()
