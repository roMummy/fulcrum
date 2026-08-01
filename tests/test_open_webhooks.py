import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select


TEMP_ROOT = Path(__file__).resolve().parents[1] / ".run" / "test-runtime"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DIR = tempfile.mkdtemp(prefix="open-webhooks-", dir=TEMP_ROOT)
os.environ["DATA_DIR"] = TEST_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DIR}/test.db"
os.environ["LOG_DIR"] = str(Path(TEST_DIR) / "logs")
Path(os.environ["LOG_DIR"]).mkdir(parents=True, exist_ok=True)

from app.core.db import Base, SessionLocal, engine
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, Channel, Conversation, Message, MessageAttachment, OpenApiApp, OpenApiAppChannel, TaskJob
from app.models.saas import Tenant
from app.services.channel_events import ingest_normalized_event
from app.services.open_webhooks import (
    build_message_event_envelope,
    deliver_open_webhook,
    list_webhook_apps_for_channel,
    process_open_webhook_delivery_job,
    sign_open_webhook_payload,
)
from app.services.open_api_auth import set_open_api_secret
from app.services.task_queue import OPEN_WEBHOOK_DELIVERY_JOB, parse_job_payload


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b"{}"


class OpenWebhookTestCase(unittest.TestCase):
    def setUp(self):
        # Webhook 测试只使用临时库，避免影响开发环境里的真实开放应用配置。
        database_path = str(getattr(engine.url, "database", "") or "")
        self.assertIn(str(TEMP_ROOT), str(Path(database_path).resolve()))
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        # SessionLocal 在这里是显式的平台旁路，只负责创建租户根实体；后续 Webhook
        # 业务数据必须进入租户感知会话，避免测试继续依赖租户化前的隐式全局数据。
        with SessionLocal() as platform_db:
            tenant = Tenant(code="open-webhook-test", name="开放 Webhook 测试租户", status="active")
            platform_db.add(tenant)
            platform_db.commit()
            platform_db.refresh(tenant)
            self.tenant_id = tenant.id

    def _tenant_db(self):
        """返回绑定当前测试租户的作用域和数据库会话上下文。"""

        # 两个上下文必须保持相同生命周期：tenant_scope 提供可信租户编号，
        # tenant_session 负责为新增对象自动补齐 tenant_id 并过滤所有业务查询。
        return tenant_scope(self.tenant_id), tenant_session(SessionLocal)

    def _create_message_graph(self, db):
        # Webhook 通过机器人主键建立归属，精简后的机器人不再维护额外 slug 标识。
        bot = Bot(name="Webhook测试机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(bot_id=bot.id, name="企微测试渠道", channel_type="workwechat", is_enabled=True, status="online")
        db.add(channel)
        db.flush()
        conversation = Conversation(
            bot_id=bot.id,
            channel_id=channel.id,
            channel_type=channel.channel_type,
            external_id="R:webhook-room",
            title="Webhook测试群",
            chat_type="group",
        )
        db.add(conversation)
        db.flush()
        message = Message(
            conversation_id=conversation.id,
            direction="in",
            sender_id="supplier-user",
            sender_name="供应商",
            content="回传文件",
            message_type="file",
            status="received",
        )
        db.add(message)
        db.flush()
        attachment = MessageAttachment(
            message_id=message.id,
            channel_id=channel.id,
            conversation_id=conversation.id,
            direction="in",
            attachment_type="file",
            provider="workwechat",
            status="downloaded",
            storage_path="/tmp/return.xls",
            mime_type="application/vnd.ms-excel",
            file_size=123,
        )
        db.add(attachment)
        app = OpenApiApp(
            name="收发单系统",
            app_key="supplier-order",
            default_channel_id=channel.id,
            status="active",
            webhook_url="http://supplier-order.test/api/mba/webhooks",
            webhook_secret="webhook-secret",
        )
        set_open_api_secret(app, "secret-001")
        db.add(app)
        db.flush()
        db.add(OpenApiAppChannel(app_id=app.id, channel_id=channel.id))
        db.commit()
        return app, channel, conversation, message, attachment

    def _create_channel_with_webhook_app(self, db):
        bot = Bot(name="Webhook入站测试机器人", status="running", is_enabled=True)
        db.add(bot)
        db.flush()
        channel = Channel(bot_id=bot.id, name="企微入站测试渠道", channel_type="workwechat", is_enabled=True, status="online")
        db.add(channel)
        db.flush()
        app = OpenApiApp(
            name="收发单系统",
            app_key="supplier-order",
            default_channel_id=channel.id,
            status="active",
            webhook_url="http://supplier-order.test/api/mba/webhooks",
            webhook_secret="webhook-secret",
        )
        set_open_api_secret(app, "secret-001")
        db.add(app)
        db.flush()
        db.add(OpenApiAppChannel(app_id=app.id, channel_id=channel.id))
        db.commit()
        db.refresh(channel)
        return app, channel

    def test_build_message_event_envelope_uses_unified_contract(self):
        tenant_context, db_context = self._tenant_db()
        with tenant_context, db_context as db:
            _app, channel, conversation, message, attachment = self._create_message_graph(db)
            envelope = build_message_event_envelope(
                event_type="file.received",
                channel=channel,
                conversation=conversation,
                message=message,
                attachments=[attachment],
            )

        self.assertEqual(envelope["event_type"], "file.received")
        self.assertEqual(envelope["channel"]["type"], "workwechat")
        self.assertEqual(envelope["conversation"]["external_id"], "R:webhook-room")
        self.assertEqual(envelope["attachments"][0]["storage_path"], "/tmp/return.xls")

    def test_list_webhook_apps_for_channel_filters_active_channel_apps(self):
        tenant_context, db_context = self._tenant_db()
        with tenant_context, db_context as db:
            app, channel, _conversation, _message, _attachment = self._create_message_graph(db)
            apps = list_webhook_apps_for_channel(db, channel)

        self.assertEqual([item.app_key for item in apps], [app.app_key])

    def test_deliver_open_webhook_sends_signature_headers(self):
        envelope = {"event_id": "evt-001", "event_type": "message.received"}
        app = OpenApiApp(app_key="supplier-order", webhook_url="http://supplier-order.test/webhook", webhook_secret="webhook-secret")
        with patch("app.services.open_webhooks.urlrequest.urlopen", return_value=_FakeResponse()) as urlopen_mock:
            result = deliver_open_webhook(app, envelope)

        self.assertTrue(result.ok)
        request = urlopen_mock.call_args.args[0]
        raw_body = request.data
        timestamp = request.headers["X-mba-timestamp"]
        self.assertEqual(request.headers["X-mba-event-id"], "evt-001")
        self.assertEqual(
            request.headers["X-mba-signature"],
            sign_open_webhook_payload("webhook-secret", raw_body, timestamp),
        )
        self.assertLess(abs(int(time.time()) - int(timestamp)), 5)

    def test_ingest_normalized_file_event_dispatches_message_and_file_webhooks(self):
        return_file = Path(TEST_DIR) / "return.xls"
        return_file.write_text(
            "<table><tr><th>子单ID</th><th>快递公司</th><th>运单号</th></tr>"
            "<tr><td>SPLIT-001</td><td>圆通</td><td>YT1234567890123</td></tr></table>",
            encoding="utf-8",
        )
        tenant_context, db_context = self._tenant_db()
        with tenant_context, db_context as db:
            _app, channel = self._create_channel_with_webhook_app(db)
            normalized = {
                "provider": "workwechat",
                "event_key": "evt-file-001",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:supplier-room",
                    "title": "供应商群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[file] 回传.xls",
                    "sender_id": "supplier-user",
                    "sender_name": "供应商",
                    "message_type": "file",
                    "external_message_id": "MSG-001",
                },
                "details": {
                    "file_name": "回传.xls",
                    "download_path": str(return_file),
                    "download_url": "http://mba.test/inbound-files/1/share/return.xls",
                    "file_size": 123,
                },
            }
            with patch("app.services.open_webhooks.urlrequest.urlopen", return_value=_FakeResponse()) as urlopen_mock:
                result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)
            jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == OPEN_WEBHOOK_DELIVERY_JOB).order_by(TaskJob.id.asc())).all()

        self.assertTrue(result["ok"])
        self.assertEqual(urlopen_mock.call_count, 0)
        self.assertEqual([parse_job_payload(job)["event_type"] for job in jobs], ["message.received", "file.received"])
        bodies = [parse_job_payload(job)["envelope"] for job in jobs]
        self.assertEqual([body["event_type"] for body in bodies], ["message.received", "file.received"])
        self.assertEqual(bodies[1]["attachments"][0]["file_name"], "回传.xls")
        self.assertEqual(bodies[1]["attachments"][0]["download_url"], "http://mba.test/inbound-files/1/share/return.xls")

    def test_open_webhook_delivery_job_posts_enqueued_envelope(self):
        tenant_context, db_context = self._tenant_db()
        with tenant_context, db_context as db:
            app, _channel = self._create_channel_with_webhook_app(db)
            payload = {
                "app_id": app.id,
                "app_key": app.app_key,
                "event_type": "message.received",
                "event_id": "evt-job-001",
                "envelope": {"event_id": "evt-job-001", "event_type": "message.received", "message": {"content": "hello"}},
            }
            with patch("app.services.open_webhooks.urlrequest.urlopen", return_value=_FakeResponse()) as urlopen_mock:
                result = process_open_webhook_delivery_job(db, payload)

        self.assertTrue(result["ok"])
        self.assertEqual(urlopen_mock.call_count, 1)
        request = urlopen_mock.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode("utf-8"))["event_id"], "evt-job-001")

    def test_supplier_order_webhook_skips_non_return_message(self):
        tenant_context, db_context = self._tenant_db()
        with tenant_context, db_context as db:
            _app, channel = self._create_channel_with_webhook_app(db)
            normalized = {
                "provider": "workwechat",
                "event_key": "evt-chat-001",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:supplier-room",
                    "title": "供应商群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "今天下午正常发货，稍后给单号",
                    "sender_id": "supplier-user",
                    "sender_name": "供应商",
                    "message_type": "text",
                    "external_message_id": "MSG-chat-001",
                },
            }
            with patch("app.services.open_webhooks.urlrequest.urlopen", return_value=_FakeResponse()) as urlopen_mock:
                result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)

            message = db.scalar(select(Message).where(Message.external_message_id == "MSG-chat-001"))
            jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == OPEN_WEBHOOK_DELIVERY_JOB)).all()
            self.assertIsNotNone(message)
            self.assertEqual(message.direction, "in")

        self.assertTrue(result["ok"])
        self.assertEqual(urlopen_mock.call_count, 0)
        self.assertEqual(jobs, [])

    def test_supplier_order_webhook_skips_downloaded_video_attachment(self):
        video_file = Path(TEST_DIR) / "supplier-video.mp4"
        # 即使视频二进制里偶然出现回单字段，也不应该进入收发单 Excel 回单解析链路。
        video_file.write_bytes(b"\x00\x00video-data \xe5\x88\x86\xe5\x8d\x95ID \xe8\xbf\x90\xe5\x8d\x95\xe5\x8f\xb7 YT1234567890123")
        tenant_context, db_context = self._tenant_db()
        with tenant_context, db_context as db:
            _app, channel = self._create_channel_with_webhook_app(db)
            normalized = {
                "provider": "workwechat",
                "event_key": "evt-video-001",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:supplier-room",
                    "title": "供应商群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[video] supplier-video.mp4",
                    "sender_id": "supplier-user",
                    "sender_name": "供应商",
                    "message_type": "video",
                    "external_message_id": "MSG-video-001",
                },
                "details": {
                    "file_name": "supplier-video.mp4",
                    "download_path": str(video_file),
                    "download_url": "http://mba.test/inbound-files/1/share/supplier-video.mp4",
                    "file_size": video_file.stat().st_size,
                },
            }
            with patch("app.services.open_webhooks.urlrequest.urlopen", return_value=_FakeResponse()) as urlopen_mock:
                result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)

            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.raw_event_id == result["raw_event_id"]))
            jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == OPEN_WEBHOOK_DELIVERY_JOB)).all()

        self.assertTrue(result["ok"])
        self.assertIsNotNone(attachment)
        self.assertEqual(attachment.attachment_type, "video")
        self.assertEqual(attachment.status, "downloaded")
        self.assertEqual(urlopen_mock.call_count, 0)
        self.assertEqual(jobs, [])

    def test_ingest_file_timeout_reuses_cached_attachment_by_hash(self):
        cached_file = Path(TEST_DIR) / "cached-return.xls"
        cached_file.write_text(
            "<table><tr><th>分单ID</th><th>快递公司</th><th>快递单号</th></tr>"
            "<tr><td>SPLIT-001</td><td>圆通</td><td>YT1234567890123</td></tr></table>",
            encoding="utf-8",
        )
        tenant_context, db_context = self._tenant_db()
        with tenant_context, db_context as db:
            _app, channel = self._create_channel_with_webhook_app(db)
            db.add(
                MessageAttachment(
                    channel_id=channel.id,
                    direction="in",
                    attachment_type="file",
                    provider="workwechat",
                    status="downloaded",
                    storage_path=str(cached_file),
                    file_size=21504,
                    metadata_json=json.dumps(
                        {
                            "file_name": "回传.xls",
                            "details": {"cdn": {"md5": "same-md5", "size": 21504}},
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            db.commit()
            normalized = {
                "provider": "workwechat",
                "event_key": "evt-file-timeout",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:supplier-room",
                    "title": "供应商群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[file] 回传.xls",
                    "sender_id": "supplier-user",
                    "sender_name": "供应商",
                    "message_type": "file",
                    "external_message_id": "MSG-timeout",
                },
                "details": {
                    "file_name": "回传.xls",
                    "download_error": "timeout",
                    "file_size": 21504,
                    "cdn": {"md5": "same-md5", "size": 21504},
                },
            }
            with patch("app.services.open_webhooks.urlrequest.urlopen", return_value=_FakeResponse()) as urlopen_mock:
                result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)

            cached = db.scalar(
                select(MessageAttachment).where(
                    MessageAttachment.channel_id == channel.id,
                    MessageAttachment.raw_event_id == result["raw_event_id"],
                )
            )
            self.assertIsNotNone(cached)
            self.assertEqual(cached.status, "downloaded")
            self.assertEqual(cached.storage_path, str(cached_file))
            self.assertIsNone(cached.last_error)
            jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == OPEN_WEBHOOK_DELIVERY_JOB).order_by(TaskJob.id.asc())).all()

        self.assertTrue(result["ok"])
        self.assertEqual(urlopen_mock.call_count, 0)
        bodies = [parse_job_payload(job)["envelope"] for job in jobs]
        self.assertEqual([body["event_type"] for body in bodies], ["message.received", "file.received"])
        self.assertEqual(bodies[1]["attachments"][0]["storage_path"], str(cached_file))


if __name__ == "__main__":
    unittest.main()
