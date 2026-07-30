import json
import os
import tempfile
import time
import unittest
from pathlib import Path


TEMP_ROOT = Path(__file__).resolve().parents[1] / ".run" / "test-runtime"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DIR = tempfile.mkdtemp(prefix="open-api-", dir=TEMP_ROOT)
os.environ["DATA_DIR"] = TEST_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DIR}/test.db"
os.environ["LOG_DIR"] = str(Path(TEST_DIR) / "logs")
Path(os.environ["LOG_DIR"]).mkdir(parents=True, exist_ok=True)

from sqlalchemy import select
from fastapi import HTTPException

from app.api.open_api import enqueue_open_file_request, enqueue_open_message_request, list_open_chatroom_options, list_open_conversation_options
from app.core.db import Base, SessionLocal, engine
from app.models.entities import Bot, Channel, Chatroom, Conversation, OpenApiApp, OpenApiAppChannel, TaskJob
from app.services.open_api_auth import (
    OpenApiAuthError,
    authenticate_open_api_request,
    set_open_api_secret,
    sign_open_api_request,
)
from app.services.task_queue import parse_job_payload


class OpenApiTestCase(unittest.TestCase):
    def setUp(self):
        # Open API 测试会重建全部表，必须限定在临时目录，避免误清开发库。
        database_path = str(getattr(engine.url, "database", "") or "")
        self.assertIn(str(TEMP_ROOT), str(Path(database_path).resolve()))
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    def _create_open_app(self, db, *, status: str = "active") -> tuple[OpenApiApp, Channel]:
        bot = Bot(name="开放接口测试机器人", slug="open-api-test", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(
            bot_id=bot.id,
            name="企微云端测试渠道",
            channel_type="workwechat",
            is_enabled=True,
            status="online",
            adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll"}, ensure_ascii=False),
        )
        db.add(channel)
        db.flush()
        app = OpenApiApp(
            name="收发单系统",
            app_key="supplier-order",
            bot_id=bot.id,
            default_channel_id=channel.id,
            status=status,
        )
        set_open_api_secret(app, "secret-001")
        db.add(app)
        db.flush()
        db.add(OpenApiAppChannel(app_id=app.id, channel_id=channel.id))
        db.commit()
        db.refresh(app)
        db.refresh(channel)
        return app, channel

    def test_authenticate_open_api_request_accepts_valid_signature(self):
        raw_body = b'{"request_id":"REQ-001"}'
        timestamp = str(int(time.time()))
        with SessionLocal() as db:
            app, _channel = self._create_open_app(db)
            result = authenticate_open_api_request(
                db,
                headers={
                    "X-App-Key": app.app_key,
                    "X-Timestamp": timestamp,
                    "X-Signature": sign_open_api_request("secret-001", raw_body, timestamp),
                },
                raw_body=raw_body,
            )

        self.assertEqual(result.app.app_key, "supplier-order")

    def test_authenticate_open_api_request_rejects_invalid_signature_and_disabled_app(self):
        raw_body = b'{"request_id":"REQ-001"}'
        timestamp = str(int(time.time()))
        with SessionLocal() as db:
            app, _channel = self._create_open_app(db, status="disabled")
            with self.assertRaises(OpenApiAuthError) as disabled_context:
                authenticate_open_api_request(
                    db,
                    headers={
                        "X-App-Key": app.app_key,
                        "X-Timestamp": timestamp,
                        "X-Signature": sign_open_api_request("secret-001", raw_body, timestamp),
                    },
                    raw_body=raw_body,
                )
            self.assertEqual(disabled_context.exception.code, "app_disabled")

            app.status = "active"
            db.commit()
            with self.assertRaises(OpenApiAuthError) as signature_context:
                authenticate_open_api_request(
                    db,
                    headers={
                        "X-App-Key": app.app_key,
                        "X-Timestamp": timestamp,
                        "X-Signature": "bad-signature",
                    },
                    raw_body=raw_body,
                )
            self.assertEqual(signature_context.exception.code, "invalid_signature")

    def test_enqueue_open_file_request_creates_cloud_job_and_is_idempotent(self):
        source_file = Path(TEST_DIR) / "order.xls"
        source_file.write_text("test order file", encoding="utf-8")
        with SessionLocal() as db:
            app, channel = self._create_open_app(db)
            first = enqueue_open_file_request(
                db,
                app=app,
                payload={
                    "request_id": "REQ-001",
                    "channel_id": channel.id,
                    "conversation_external_id": "R:supplier-room",
                    "conversation_title": "供应商群",
                    "chat_type": "group",
                    "file_name": "order.xls",
                    "file_path": str(source_file),
                    "content": "供应商发单批次",
                },
                public_base_url="http://testserver",
            )
            db.commit()
            second = enqueue_open_file_request(
                db,
                app=app,
                payload={
                    "request_id": "REQ-001",
                    "channel_id": channel.id,
                    "conversation_external_id": "R:supplier-room",
                    "conversation_title": "供应商群",
                    "chat_type": "group",
                    "file_name": "order.xls",
                    "file_path": str(source_file),
                    "content": "供应商发单批次",
                },
                public_base_url="http://testserver",
            )
            db.commit()

            self.assertTrue(first["queued"])
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["job"]["id"], second["job"]["id"])

            conversation = db.scalar(select(Conversation).where(Conversation.external_id == "R:supplier-room"))
            self.assertIsNotNone(conversation)
            job = db.get(TaskJob, first["job"]["id"])
            self.assertIsNotNone(job)
            self.assertEqual(job.job_type, "workwechat_cloud_outbound")
            payload = parse_job_payload(job)
            self.assertEqual(payload["channel_id"], channel.id)
            self.assertEqual(payload["conversation_id"], conversation.id)
            self.assertEqual(payload["open_api"]["request_id"], "REQ-001")
            self.assertEqual(payload["open_api"]["app_key"], "supplier-order")
            self.assertIn("/outbound-files/", payload["payload"]["file_url"])

    def test_enqueue_open_message_request_creates_text_job_and_is_idempotent(self):
        with SessionLocal() as db:
            app, channel = self._create_open_app(db)
            first = enqueue_open_message_request(
                db,
                app=app,
                payload={
                    "request_id": "MSG-REQ-001",
                    "channel_id": channel.id,
                    "conversation_external_id": "R:service-room",
                    "conversation_title": "客服群",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": "订单已经发出",
                },
            )
            db.commit()
            second = enqueue_open_message_request(
                db,
                app=app,
                payload={
                    "request_id": "MSG-REQ-001",
                    "channel_id": channel.id,
                    "conversation_external_id": "R:service-room",
                    "conversation_title": "客服群",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": "订单已经发出",
                },
            )
            db.commit()

            self.assertTrue(first["queued"])
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            job = db.get(TaskJob, first["job"]["id"])
            self.assertIsNotNone(job)
            self.assertEqual(job.job_type, "workwechat_cloud_outbound")
            payload = parse_job_payload(job)
            self.assertEqual(payload["channel_id"], channel.id)
            self.assertEqual(payload["payload"]["message_type"], "text")
            self.assertEqual(payload["payload"]["content"], "订单已经发出")
            self.assertEqual(payload["open_api"]["request_id"], "MSG-REQ-001")

    def test_enqueue_open_message_request_requires_authorized_channel_id(self):
        with SessionLocal() as db:
            app, _channel = self._create_open_app(db)
            with self.assertRaises(HTTPException) as missing_context:
                enqueue_open_message_request(
                    db,
                    app=app,
                    payload={
                        "request_id": "MSG-REQ-MISSING",
                        "conversation_external_id": "R:service-room",
                        "message_type": "text",
                        "content": "订单已经发出",
                    },
                )
            self.assertEqual(missing_context.exception.detail, "channel_id_required")

            with self.assertRaises(HTTPException) as forbidden_context:
                enqueue_open_message_request(
                    db,
                    app=app,
                    payload={
                        "request_id": "MSG-REQ-FORBIDDEN",
                        "channel_id": 9999,
                        "conversation_external_id": "R:service-room",
                        "message_type": "text",
                        "content": "订单已经发出",
                    },
                )
            self.assertEqual(forbidden_context.exception.detail, "channel_not_allowed")

    def test_open_api_app_can_use_authorized_channel_from_another_bot(self):
        with SessionLocal() as db:
            app, _channel = self._create_open_app(db)
            other_bot = Bot(name="另一个开放接口机器人", slug="open-api-other-bot", status="running", is_enabled=True)
            db.add(other_bot)
            db.flush()
            other_channel = Channel(
                bot_id=other_bot.id,
                name="另一个企微渠道",
                channel_type="workwechat",
                is_enabled=True,
                status="online",
                adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll"}, ensure_ascii=False),
            )
            db.add(other_channel)
            db.flush()
            db.add(OpenApiAppChannel(app_id=app.id, channel_id=other_channel.id))
            chatroom = Chatroom(
                bot_id=other_bot.id,
                channel_id=other_channel.id,
                external_id="R:other-bot-room",
                name="另一个机器人供应商群",
                status="active",
                is_active=True,
            )
            db.add(chatroom)
            db.commit()

            items = list_open_chatroom_options(db, app=app)
            result = enqueue_open_message_request(
                db,
                app=app,
                payload={
                    "request_id": "MSG-REQ-OTHER-BOT",
                    "channel_id": other_channel.id,
                    "conversation_external_id": "R:other-bot-room",
                    "conversation_title": "另一个机器人供应商群",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": "跨机器人授权渠道发送测试",
                },
            )

            self.assertIn(other_channel.id, {item["channel_id"] for item in items})
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.channel_id == other_channel.id,
                    Conversation.external_id == "R:other-bot-room",
                )
            )
            self.assertIsNotNone(conversation)
            self.assertEqual(conversation.bot_id, other_bot.id)
            self.assertEqual(result["channel_id"], other_channel.id)

    def test_list_open_conversation_options_merges_conversation_and_chatroom_ids(self):
        with SessionLocal() as db:
            app, channel = self._create_open_app(db)
            conversation = Conversation(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                channel_type=channel.channel_type,
                external_id="R:supplier-room",
                title="供应商群",
                chat_type="group",
            )
            db.add(conversation)
            db.flush()
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="R:supplier-room",
                name="供应商群登记",
                status="active",
            )
            db.add(chatroom)
            db.commit()

            items = list_open_conversation_options(db, app=app, chat_type="group")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["bot_id"], channel.bot_id)
        self.assertEqual(items[0]["channel_id"], channel.id)
        self.assertEqual(items[0]["conversation_id"], conversation.id)
        self.assertEqual(items[0]["chatroom_id"], chatroom.id)
        self.assertEqual(items[0]["conversation_external_id"], "R:supplier-room")

    def test_list_open_chatroom_options_returns_authorized_channel_groups(self):
        with SessionLocal() as db:
            app, channel = self._create_open_app(db)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="R:supplier-room",
                name="供应商群",
                status="active",
                is_active=True,
                member_count=12,
            )
            db.add(chatroom)
            db.commit()

            items = list_open_chatroom_options(db, app=app)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["channel_id"], channel.id)
        self.assertEqual(items[0]["chatroom_id"], chatroom.id)
        self.assertEqual(items[0]["chatroom_external_id"], "R:supplier-room")
        self.assertEqual(items[0]["member_count"], 12)


if __name__ == "__main__":
    unittest.main()
