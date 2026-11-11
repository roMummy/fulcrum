from __future__ import annotations

import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, Chatroom, ChatroomMember, Conversation, Message, MessageAttachment, OpenApiApp, OpenApiAppChannel, TaskJob, WorkWechatContact
from app.models.saas import Tenant
from app.services.open_api_auth import set_open_api_secret
from app.services.channel_adapters.workwechat import WorkWechatAdapter
from app.services.workwechat_cloud_poll import (
    build_token_hash,
    claim_cloud_jobs,
    complete_cloud_job,
    exchange_authorization_code,
    fail_cloud_job,
    find_channel_by_authorization_code,
    find_channel_by_client_token,
    issue_authorization_code,
    issue_channel_authorization_code,
    is_cloud_poll_enabled,
    load_cloud_poll_config,
    recover_stale_cloud_jobs,
    resolve_outbound_job_type,
    verify_client_token,
)
from app.services.runtime import run_channel_health_check
from app.services.security import encrypt_secret
from app.services.task_queue import OPEN_WEBHOOK_DELIVERY_JOB, OUTBOUND_DISPATCH_JOB, WORKWECHAT_CLOUD_CONTROL_JOB, WORKWECHAT_CLOUD_OUTBOUND_JOB, parse_job_payload
from app.services.task_queue import enqueue_job
from app.services.workwechat_ws_sessions import WorkWechatWebSocketRegistry
from app.api.workwechat_provider_cloud import _find_pending_cloud_job_id


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b"{}"


class WorkWechatCloudPollTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        with TestingSession() as platform_db:
            tenant = Tenant(code="cloud-poll-test", name="云轮询测试租户", status="active")
            platform_db.add(tenant)
            platform_db.commit()
            platform_db.refresh(tenant)
            self.tenant_id = tenant.id

        # 云轮询服务操作的机器人、渠道和任务都属于租户业务数据。测试夹具进入与
        # 正式请求相同的可信租户上下文，依靠会话级隔离自动补齐并校验 tenant_id。
        self.tenant_scope_context = tenant_scope(self.tenant_id)
        self.tenant_scope_context.__enter__()
        self.tenant_db_context = tenant_session(TestingSession)
        self.db = self.tenant_db_context.__enter__()

        # 云轮询只依赖机器人主键；测试夹具不得重新引入已从产品模型删除的 slug。
        bot = Bot(name="Bot")
        self.db.add(bot)
        self.db.flush()
        self.channel = Channel(bot_id=bot.id, name="企微", channel_type="workwechat")
        self.db.add(self.channel)
        self.db.commit()
        self.bot = bot

    def tearDown(self):
        self.tenant_db_context.__exit__(None, None, None)
        self.tenant_scope_context.__exit__(None, None, None)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_cloud_poll_enabled_only_for_workwechat_cloud_poll_mode(self):
        channel = Channel(channel_type="workwechat", adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll"}))

        self.assertTrue(is_cloud_poll_enabled(channel))

    def test_cloud_poll_not_enabled_for_direct_http(self):
        channel = Channel(channel_type="workwechat", adapter_config_json=json.dumps({"workwechat_delivery_mode": "direct_http"}))

        self.assertFalse(is_cloud_poll_enabled(channel))

    def test_build_token_hash_is_stable_and_not_plaintext(self):
        digest = build_token_hash("secret-token")

        self.assertNotEqual(digest, "secret-token")
        self.assertEqual(digest, build_token_hash("secret-token"))

    def test_load_cloud_poll_config_has_defaults(self):
        channel = Channel(channel_type="workwechat", adapter_config_json="")

        config = load_cloud_poll_config(channel)

        self.assertEqual(config["poll_batch_size"], 5)
        self.assertEqual(config["poll_interval_seconds"], 3)
        self.assertEqual(config["running_lease_seconds"], 900)

    def test_resolve_outbound_job_type_uses_cloud_type_only_for_cloud_poll(self):
        cloud_channel = Channel(
            channel_type="workwechat",
            adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll"}),
        )
        direct_channel = Channel(
            channel_type="workwechat",
            adapter_config_json=json.dumps({"workwechat_delivery_mode": "direct_http"}),
        )

        self.assertEqual(resolve_outbound_job_type(cloud_channel), WORKWECHAT_CLOUD_OUTBOUND_JOB)
        self.assertEqual(resolve_outbound_job_type(direct_channel), OUTBOUND_DISPATCH_JOB)

    def test_issue_authorization_code_stores_hash_and_expiry(self):
        code = issue_authorization_code(self.channel)
        config = load_cloud_poll_config(self.channel)

        self.assertTrue(code.startswith("wwauth_"))
        self.assertEqual(config["workwechat_delivery_mode"], "cloud_poll")
        self.assertEqual(config["authorization_code_hash"], build_token_hash(code))
        self.assertIn("authorization_code_expires_at", config)

    def test_issue_channel_authorization_code_enables_cloud_mode(self):
        _, code = issue_channel_authorization_code(self.db, self.channel.id)
        self.db.refresh(self.channel)
        config = load_cloud_poll_config(self.channel)

        self.assertTrue(code.startswith("wwauth_"))
        self.assertEqual(config["workwechat_delivery_mode"], "cloud_poll")
        self.assertEqual(config["authorization_code_hash"], build_token_hash(code))

    def test_cloud_poll_health_check_does_not_require_api_base_or_webhook(self):
        self.channel.bot_token_encrypted = encrypt_secret("channel-secret")
        self.channel.api_base = None
        self.channel.webhook_path = None
        issue_authorization_code(self.channel)
        self.db.commit()

        status, message = run_channel_health_check(self.db, self.channel)

        self.assertEqual(status, "online")
        self.assertIn("不需要 API Base", message)

    def test_workwechat_adapter_cloud_poll_health_check_does_not_require_api_base(self):
        self.channel.bot_token_encrypted = encrypt_secret("channel-secret")
        self.channel.api_base = None
        self.channel.webhook_path = None
        code = issue_authorization_code(self.channel)
        exchange_authorization_code(self.channel, code)
        self.db.commit()

        result = WorkWechatAdapter().health_check(self.channel)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "online")
        self.assertIn("不需要 API Base", result["summary"])
        self.assertFalse(result["details"]["api_base_required"])

    def test_direct_http_health_check_still_requires_api_base(self):
        self.channel.bot_token_encrypted = encrypt_secret("channel-secret")
        self.channel.api_base = None
        self.channel.webhook_path = None
        self.channel.adapter_config_json = json.dumps({"workwechat_delivery_mode": "direct_http"})
        self.db.commit()

        status, message = run_channel_health_check(self.db, self.channel)

        self.assertEqual(status, "degraded")
        self.assertIn("API Base 缺失", message)

    def test_issue_channel_authorization_code_rejects_non_workwechat_channel(self):
        channel = Channel(bot_id=self.bot.id, name="其他渠道", channel_type="telegram")
        self.db.add(channel)
        self.db.commit()

        with self.assertRaises(ValueError):
            issue_channel_authorization_code(self.db, channel.id)

    def test_exchange_authorization_code_returns_client_token_once(self):
        code = issue_authorization_code(self.channel)

        result = exchange_authorization_code(
            self.channel,
            code,
            provider_name="客服电脑A",
            client_version="5.0.0.6008",
        )

        self.assertTrue(result["client_token"].startswith("wwclient_"))
        self.assertEqual(result["channel_id"], self.channel.id)
        self.assertTrue(verify_client_token(self.channel, result["client_token"]))
        with self.assertRaises(ValueError):
            exchange_authorization_code(
                self.channel,
                code,
                provider_name="客服电脑A",
                client_version="5.0.0.6008",
            )

    def test_find_channel_by_authorization_code(self):
        code = issue_authorization_code(self.channel)
        self.db.commit()

        found = find_channel_by_authorization_code(self.db, code)

        self.assertIsNotNone(found)
        self.assertEqual(found.id, self.channel.id)

    def test_authorization_code_is_enough_to_resolve_channel(self):
        code = issue_authorization_code(self.channel)
        self.db.commit()

        found = find_channel_by_authorization_code(self.db, code)
        result = exchange_authorization_code(
            found,
            code,
            provider_name="通用客户端",
            client_version="5.0.0.6008",
        )

        self.assertEqual(result["channel_id"], self.channel.id)
        self.assertNotIn("required_channel_id_from_client", result)

    def test_find_channel_by_client_token(self):
        code = issue_authorization_code(self.channel)
        result = exchange_authorization_code(
            self.channel,
            code,
            provider_name="客服电脑A",
            client_version="5.0.0.6008",
        )
        self.db.commit()

        found = find_channel_by_client_token(self.db, result["client_token"])

        self.assertIsNotNone(found)
        self.assertEqual(found.id, self.channel.id)

    def test_ws_registry_noops_without_connected_client(self):
        registry = WorkWechatWebSocketRegistry()

        result = registry.notify_job_available(channel_id=self.channel.id, job_id=123)

        self.assertEqual(result["sent"], 0)

    def test_claim_cloud_jobs_returns_only_channel_jobs_and_marks_running(self):
        conversation = self._create_conversation()
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={"channel_id": self.channel.id, "conversation_id": conversation.id, "payload": {"message_type": "text", "content": "hello"}},
            priority=90,
        )
        other_channel = Channel(bot_id=self.bot.id, name="其他企微", channel_type="workwechat")
        self.db.add(other_channel)
        self.db.flush()
        enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={"channel_id": other_channel.id, "conversation_id": conversation.id, "payload": {"message_type": "text", "content": "skip"}},
            priority=90,
        )
        self.db.commit()

        jobs = claim_cloud_jobs(self.db, self.channel, batch_size=5)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], job.id)
        self.assertEqual(jobs[0]["conversation_external_id"], "R:room-001")
        self.assertIn("payload_hash", jobs[0])
        refreshed = self.db.get(TaskJob, job.id)
        self.assertEqual(refreshed.status, "running")
        self.assertEqual(refreshed.attempts, 1)

    def test_recover_stale_cloud_outbound_marks_unconfirmed_without_requeue(self):
        conversation = self._create_conversation()
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={"channel_id": self.channel.id, "conversation_id": conversation.id, "payload": {"message_type": "file", "content": "订单.xlsx"}},
            priority=90,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)
        refreshed = self.db.get(TaskJob, job.id)
        refreshed.locked_at = now_shanghai() - timedelta(seconds=120)
        self.db.commit()

        recovered = recover_stale_cloud_jobs(self.db, self.channel, stale_seconds=60)

        self.assertEqual(recovered, 1)
        self.db.refresh(refreshed)
        self.assertEqual(refreshed.status, "completed")
        self.assertIsNotNone(refreshed.finished_at)
        self.assertIsNone(refreshed.error_message)
        self.assertEqual(refreshed.attempts, 1)
        message = self.db.query(Message).filter_by(direction="out").one()
        self.assertEqual(message.status, "sent")
        self.assertEqual(message.external_status, "accepted_unconfirmed")
        result = json.loads(refreshed.result_json)
        self.assertEqual(result["adapter_result"]["error_code"], "cloud_provider_lease_expired")
        self.assertIn("未自动重发", result["adapter_result"]["error_summary"])

    def test_recover_stale_cloud_control_job_still_requeues(self):
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
            payload={"channel_id": self.channel.id, "action": "sync_chatrooms", "payload": {}},
            priority=90,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)
        refreshed = self.db.get(TaskJob, job.id)
        refreshed.locked_at = now_shanghai() - timedelta(seconds=120)
        self.db.commit()

        recovered = recover_stale_cloud_jobs(self.db, self.channel, stale_seconds=60)

        self.assertEqual(recovered, 1)
        self.db.refresh(refreshed)
        self.assertEqual(refreshed.status, "pending")
        self.assertIsNone(refreshed.started_at)
        self.assertIsNone(refreshed.locked_at)
        self.assertEqual(refreshed.error_message, "cloud_provider_lease_expired_requeued")

    def test_find_pending_cloud_job_id_returns_channel_specific_job(self):
        conversation = self._create_conversation()
        other_channel = Channel(bot_id=self.bot.id, name="其它企微", channel_type="workwechat", adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll"}))
        self.db.add(other_channel)
        self.db.flush()
        enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={"channel_id": other_channel.id, "conversation_id": conversation.id, "payload": {"message_type": "text", "content": "other"}},
            priority=100,
        )
        expected = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={"channel_id": self.channel.id, "conversation_id": conversation.id, "payload": {"message_type": "text", "content": "mine"}},
            priority=90,
        )
        self.db.commit()

        self.assertEqual(_find_pending_cloud_job_id(self.db, channel_id=self.channel.id), expected.id)

    def test_complete_cloud_job_marks_job_completed_and_records_message(self):
        conversation = self._create_conversation()
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={"channel_id": self.channel.id, "conversation_id": conversation.id, "payload": {"message_type": "text", "content": "done"}},
            priority=90,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)

        result = complete_cloud_job(
            self.db,
            self.channel,
            job.id,
            {
                "adapter_result": {
                    "ok": True,
                    "status": "sent",
                    "external_message_id": "ww-msg-1",
                }
            },
        )

        self.assertTrue(result["ok"])
        refreshed = self.db.get(TaskJob, job.id)
        self.assertEqual(refreshed.status, "completed")
        message = self.db.query(Message).one()
        self.assertEqual(message.content, "done")
        self.assertEqual(message.external_message_id, "ww-msg-1")

    def test_cloud_control_chatroom_sync_job_materializes_provider_rooms(self):
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
            payload={"channel_id": self.channel.id, "action": "sync_chatrooms", "payload": {"page_size": 100}},
            priority=95,
        )
        self.db.commit()

        jobs = claim_cloud_jobs(self.db, self.channel, batch_size=1)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["action"], "sync_chatrooms")
        self.assertNotIn("conversation_id", jobs[0])
        result = complete_cloud_job(
            self.db,
            self.channel,
            job.id,
            {
                "adapter_result": {
                    "ok": True,
                    "rooms": [
                        {
                            "conversation_id": "R:cloud-room",
                            "nickname": "云端同步群",
                            "create_user_id": "owner-001",
                            "total": 3,
                        }
                    ],
                }
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 1)
        chatroom = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:cloud-room").one()
        self.assertEqual(chatroom.name, "云端同步群")
        self.assertEqual(chatroom.member_count, 3)

    def test_targeted_cloud_chatroom_sync_only_updates_current_room(self):
        chatroom = Chatroom(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            external_id="R:target-cloud-room",
            name="R:target-cloud-room",
            status="message_observed",
        )
        self.db.add(chatroom)
        self.db.flush()
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
            payload={
                "channel_id": self.channel.id,
                "action": "sync_chatrooms",
                "target_chatroom_id": chatroom.id,
                "target_external_id": chatroom.external_id,
                "payload": {"page_size": 100, "target_external_id": chatroom.external_id},
            },
            priority=95,
            related_type="chatroom",
            related_id=chatroom.id,
        )
        self.db.commit()

        result = complete_cloud_job(
            self.db,
            self.channel,
            job.id,
            {
                "adapter_result": {
                    "ok": True,
                    "rooms": [
                        {"conversation_id": "R:target-cloud-room", "nickname": "云端目标群", "total": 5},
                        {"conversation_id": "R:other-cloud-room", "nickname": "不应落库群", "total": 2},
                    ],
                }
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["target_external_id"], "R:target-cloud-room")
        self.assertEqual(chatroom.name, "云端目标群")
        self.assertEqual(self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="R:other-cloud-room").count(), 0)

    def test_cloud_control_chatroom_member_sync_job_materializes_members(self):
        chatroom = Chatroom(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            external_id="R:cloud-room",
            name="云端同步群",
            status="profile_synced",
        )
        self.db.add(chatroom)
        self.db.flush()
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
            payload={
                "channel_id": self.channel.id,
                "action": "sync_chatroom_members",
                "chatroom_id": chatroom.id,
                "payload": {"conversation_id": chatroom.external_id},
            },
            priority=95,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)

        result = complete_cloud_job(
            self.db,
            self.channel,
            job.id,
            {
                "adapter_result": {
                    "ok": True,
                    "members": [
                        {
                            "user_id": "member-001",
                            "username": "甲",
                            "is_admin": 1,
                        }
                    ],
                }
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["synced_members"], 1)
        member = self.db.query(ChatroomMember).filter_by(chatroom_id=chatroom.id, member_external_id="member-001").one()
        self.assertEqual(member.display_name, "甲")
        self.assertEqual(member.role, "admin")

    def test_cloud_control_contact_sync_job_materializes_contacts(self):
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
            payload={"channel_id": self.channel.id, "action": "sync_contacts", "payload": {"contact_kind": "all"}},
            priority=95,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)

        result = complete_cloud_job(
            self.db,
            self.channel,
            job.id,
            {
                "adapter_result": {
                    "ok": True,
                    "contacts": [
                        {"userid": "staff-001", "name": "内部成员", "contact_kind": "internal_member"},
                        {"external_userid": "external-001", "name": "外部联系人", "contact_kind": "external_contact"},
                    ],
                }
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["synced_contacts"], 2)
        contacts = self.db.query(WorkWechatContact).order_by(WorkWechatContact.contact_external_id.asc()).all()
        self.assertEqual([item.contact_external_id for item in contacts], ["external-001", "staff-001"])

    def test_cloud_control_download_inbound_media_updates_attachment_and_dispatches_file_event(self):
        conversation = self._create_conversation()
        message = Message(
            conversation_id=conversation.id,
            direction="in",
            sender_id="supplier-user",
            sender_name="供应商",
            content="[file] 回单.xlsx",
            message_type="file",
            external_message_id="MSG-return-001",
            status="received",
        )
        self.db.add(message)
        self.db.flush()
        attachment = MessageAttachment(
            message_id=message.id,
            channel_id=self.channel.id,
            conversation_id=conversation.id,
            direction="in",
            attachment_type="file",
            provider="workwechat",
            status="pending",
            file_size=4926,
            last_error="download_inbound_file_not_found: C:\\cache\\回单.xlsx",
            metadata_json=json.dumps({"file_name": "回单.xlsx"}, ensure_ascii=False),
        )
        self.db.add(attachment)
        app = OpenApiApp(
            name="普通开放应用",
            app_key="ops-webhook",
            default_channel_id=self.channel.id,
            status="active",
            webhook_url="http://ops-webhook.test/api/mba/webhooks",
            webhook_secret="webhook-secret",
        )
        set_open_api_secret(app, "secret-001")
        self.db.add(app)
        self.db.flush()
        self.db.add(OpenApiAppChannel(app_id=app.id, channel_id=self.channel.id))
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_CONTROL_JOB,
            payload={
                "channel_id": self.channel.id,
                "action": "download_inbound_media",
                "attachment_id": attachment.id,
                "payload": {"attachment_id": attachment.id, "message_type": "file", "data": {"cdn": {"file_id": "file-id", "aes_key": "aes-key"}}},
            },
            priority=96,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)

        result = complete_cloud_job(
            self.db,
            self.channel,
            job.id,
            {
                "adapter_result": {
                    "ok": True,
                    "download_path": "/home/mba-stack/fulcrum/data/inbound_files/2/share/回单.xlsx",
                    "download_url": "http://mba.test/inbound-files/2/share/return.xlsx",
                    "file_size": 4926,
                    "mime_type": "application/octet-stream",
                }
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "download_inbound_media")
        self.db.refresh(attachment)
        self.assertEqual(attachment.status, "downloaded")
        self.assertEqual(attachment.storage_path, "/home/mba-stack/fulcrum/data/inbound_files/2/share/回单.xlsx")
        self.assertIsNone(attachment.last_error)
        webhook_job = self.db.query(TaskJob).filter_by(job_type=OPEN_WEBHOOK_DELIVERY_JOB).one()
        envelope = parse_job_payload(webhook_job)["envelope"]
        self.assertEqual(envelope["event_type"], "file.received")
        self.assertEqual(envelope["attachments"][0]["storage_path"], attachment.storage_path)

    def test_complete_cloud_job_dispatches_open_api_send_result(self):
        conversation = self._create_conversation()
        app = OpenApiApp(
            name="收发单系统",
            app_key="supplier-order",
            default_channel_id=self.channel.id,
            status="active",
            webhook_url="http://supplier-order.test/api/mba/webhooks",
            webhook_secret="webhook-secret",
        )
        set_open_api_secret(app, "secret-001")
        self.db.add(app)
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={
                "channel_id": self.channel.id,
                "conversation_id": conversation.id,
                "payload": {"message_type": "file", "content": "发单文件"},
                "open_api": {"app_key": "supplier-order", "request_id": "supplier-order-batch-1"},
            },
            priority=90,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)

        with patch("app.services.open_webhooks.urlrequest.urlopen", return_value=_FakeResponse()) as urlopen_mock:
            complete_cloud_job(
                self.db,
                self.channel,
                job.id,
                {
                    "adapter_result": {
                        "ok": True,
                        "status": "sent",
                        "external_message_id": "ww-msg-1",
                    }
                },
            )

        self.assertEqual(urlopen_mock.call_count, 0)
        webhook_job = self.db.query(TaskJob).filter_by(job_type=OPEN_WEBHOOK_DELIVERY_JOB).one()
        body = parse_job_payload(webhook_job)["envelope"]
        self.assertEqual(body["event_type"], "message.sent")
        self.assertEqual(body["open_api"]["request_id"], "supplier-order-batch-1")
        self.assertEqual(body["open_api"]["job_id"], job.id)

    def test_fail_cloud_outbound_job_does_not_requeue_when_attempts_remain(self):
        conversation = self._create_conversation()
        app = OpenApiApp(
            name="收发单系统",
            app_key="supplier-order",
            default_channel_id=self.channel.id,
            status="active",
            webhook_url="http://supplier-order.test/api/mba/webhooks",
            webhook_secret="webhook-secret",
        )
        set_open_api_secret(app, "secret-001")
        self.db.add(app)
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={
                "channel_id": self.channel.id,
                "conversation_id": conversation.id,
                "payload": {"message_type": "text", "content": "fail"},
                "open_api": {"app_key": "supplier-order", "request_id": "supplier-order-batch-fail"},
            },
            priority=90,
            max_attempts=2,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)

        result = fail_cloud_job(
            self.db,
            self.channel,
            job.id,
            error_code="hook_send_failed",
            error_message="Hook 发送失败",
            adapter_result={"ok": False},
        )

        self.assertFalse(result["ok"])
        refreshed = self.db.get(TaskJob, job.id)
        self.assertEqual(refreshed.status, "failed")
        self.assertIsNotNone(refreshed.finished_at)
        self.assertIn("Hook 发送失败", refreshed.error_message)
        webhook_job = self.db.query(TaskJob).filter_by(job_type=OPEN_WEBHOOK_DELIVERY_JOB).one()
        envelope = parse_job_payload(webhook_job)["envelope"]
        self.assertEqual(envelope["event_type"], "message.failed")
        self.assertEqual(envelope["delivery"]["status"], "failed")
        self.assertTrue(envelope["delivery"]["requires_attention"])
        self.assertIn("未自动重发", envelope["job"]["error_summary"])

    def test_delivery_confirmation_timeout_completes_without_retry(self):
        conversation = self._create_conversation()
        app = OpenApiApp(
            name="收发单系统",
            app_key="supplier-order",
            default_channel_id=self.channel.id,
            status="active",
            webhook_url="http://supplier-order.test/api/mba/webhooks",
            webhook_secret="webhook-secret",
        )
        set_open_api_secret(app, "secret-001")
        self.db.add(app)
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={
                "channel_id": self.channel.id,
                "conversation_id": conversation.id,
                "payload": {"message_type": "text", "content": "maybe delivered"},
                "open_api": {"app_key": "supplier-order", "request_id": "supplier-order-batch-timeout"},
            },
            priority=90,
            max_attempts=3,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)

        result = fail_cloud_job(
            self.db,
            self.channel,
            job.id,
            error_code="delivery_confirmation_timeout",
            error_message="delivery_confirmation_timeout",
            adapter_result={
                "ok": False,
                "status": "delivery_confirmation_timeout",
                "external_status": "accepted_unconfirmed",
            },
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["external_status"], "accepted_unconfirmed")
        refreshed = self.db.get(TaskJob, job.id)
        self.assertEqual(refreshed.status, "completed")
        self.assertEqual(refreshed.attempts, 1)
        message = self.db.query(Message).filter_by(direction="out").one()
        self.assertEqual(message.status, "sent")
        self.assertEqual(message.external_status, "accepted_unconfirmed")
        webhook_job = self.db.query(TaskJob).filter_by(job_type=OPEN_WEBHOOK_DELIVERY_JOB).one()
        envelope = parse_job_payload(webhook_job)["envelope"]
        self.assertEqual(envelope["event_type"], "message.sent")
        self.assertEqual(envelope["delivery"]["status"], "accepted_unconfirmed")
        self.assertFalse(envelope["delivery"]["confirmed"])
        self.assertTrue(envelope["delivery"]["requires_attention"])
        self.assertIn("未确认", envelope["job"]["error_summary"])

    def test_fail_cloud_job_is_idempotent_for_completed_job(self):
        conversation = self._create_conversation()
        job = enqueue_job(
            self.db,
            job_type=WORKWECHAT_CLOUD_OUTBOUND_JOB,
            payload={
                "channel_id": self.channel.id,
                "conversation_id": conversation.id,
                "payload": {"message_type": "text", "content": "already done"},
            },
            priority=90,
        )
        self.db.commit()
        claim_cloud_jobs(self.db, self.channel, batch_size=1)
        complete_cloud_job(
            self.db,
            self.channel,
            job.id,
            {"adapter_result": {"ok": True, "status": "sent", "external_message_id": "ww-msg-1"}},
        )

        result = fail_cloud_job(
            self.db,
            self.channel,
            job.id,
            error_code="delivery_confirmation_timeout",
            error_message="late timeout",
            adapter_result={"ok": False, "status": "delivery_confirmation_timeout"},
        )

        self.assertTrue(result["idempotent"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.db.query(Message).filter_by(direction="out").count(), 1)

    def _create_conversation(self) -> Conversation:
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="workwechat",
            external_id="R:room-001",
            title="测试群",
            chat_type="group",
        )
        self.db.add(conversation)
        self.db.commit()
        return conversation
