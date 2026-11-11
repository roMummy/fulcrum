import base64
import asyncio
import json
import os
import tempfile
import time
import unittest
import warnings
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock, patch


TEMP_ROOT = Path(__file__).resolve().parents[1] / ".run" / "test-runtime"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DIR = tempfile.mkdtemp(prefix="fulcrum-", dir=TEMP_ROOT)
os.environ["DATA_DIR"] = TEST_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DIR}/test.db"
os.environ["AUTH_SEED_DEMO_USERS"] = "true"

LOG_DIR = Path(TEST_DIR).resolve().parents[0] / "logs"
os.environ["LOG_DIR"] = str(LOG_DIR)
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.joinpath("app.log").write_text(
    "2026-03-22 10:00:00 | INFO | test.app | hello info line\n",
    encoding="utf-8",
)
LOG_DIR.joinpath("error.log").write_text(
    "2026-03-22 10:05:00 | ERROR | test.error | webhook signature failed\n",
    encoding="utf-8",
)
LOG_DIR.joinpath("background-check.log").write_text(
    "[2026-03-22 10:00:00] background-check warn: channel degraded\n"
    "[2026-03-22 10:05:00] background-check error: webhook signature failed\n",
    encoding="utf-8",
)

from fastapi.testclient import TestClient
from starlette.requests import Request
from sqlalchemy import create_engine, desc, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.core.db import Base, engine
from app.main import app, templates
from app.core.db import SessionLocal
from app.core.settings import settings
from app.models.entities import AdminUser, Bot, BotPlugin, Channel, Chatroom, ChatroomMember, Conversation, GroupPluginScope, HealthCheckRecord, Message, Plugin, PointsAccount, PointsPrisonRecord, PointsTransaction, RawWebhookEvent, SignInHistory, SignInRecord, TaskJob
from app.services import async_workers
import app.services.init_db as init_db_service
from app.services.auth import AUTH_COOKIE_NAME, build_auth_cookie, hash_password
from app.services.channel_events import ingest_inbound_event
from app.services.init_db import init_db_with_seed
from app.services.plugin_directory import (
    LoadedDirectoryPlugin,
    _build_auto_outbound_payloads,
    build_dispatch_report,
    build_static_dispatch_plan,
    discover_plugins,
    load_directory_plugin,
    sync_plugins_from_directory,
)
from app.services.plugin_runtime import get_plugin_runtime_definition, get_plugin_runtime_meta, list_plugin_runtime_definitions
from app.services.plugin_runtime.contracts import PluginExecutionRequest
from app.services.plugin_runtime.http_client import PluginHttpResponse
from app.services.plugin_schemas import build_plugin_config_view, get_plugin_config_schema
from app.services.task_queue import enqueue_job
from app.services.seed import seed_auth_users
from app.services.security import build_telegram_webhook_secret, encrypt_secret, mask_secret, sign_webhook_payload
from app.services.workwechat_cloud_poll import exchange_authorization_code, issue_authorization_code
from app.services.wechat_ipad.client import WechatIpadApiClient
from app.services.wechat_ipad.contract import IPAD_874_CONTRACT
import app.services.wechat_ipad.service as wechat_ipad_service
from app.services.wechat_ipad.service import build_wechat_ipad_group_summary, build_wechat_ipad_overview, refresh_wechat_ipad_status
from app.services.wechat_ipad.chatrooms import upsert_wechat_ipad_chatroom_profile


def seed_legacy_test_fixture(db):
    """仅为历史回归构造确定性业务数据，不恢复生产环境已经删除的演示数据。"""

    # 大量历史用例通过固定主键访问机器人和渠道。这里完整复刻测试真正依赖的最小关系，
    # 同时遵守当前机器人精简契约，不再创建分组、模板、标签、slug 或环境字段。
    bot = Bot(
        name="ABOT-主机器人",
        description="历史回归测试专用机器人",
        status="running",
        is_enabled=True,
    )
    db.add(bot)
    db.flush()

    channels = [
        Channel(
            bot_id=bot.id,
            name="微信 iPad 通道",
            channel_type="wechat_ipad",
            api_base="http://192.168.1.170:8059",
            bot_token_encrypted=encrypt_secret("wx-demo-token-main"),
            bot_token_masked=mask_secret("wx-demo-token-main"),
            webhook_path="/wechat/webhook",
            dm_policy="allowlist",
            allow_from="demo_user_b",
            adapter_config_json='{"server_url":"http://192.168.1.170:8059/","wxid":"wxid_demo_account_001","device_id":"4978fc0fd191cf45a4e55fae7936b153","device_name":"Demo iPad Device","server_ip":"192.168.1.170","server_port":"8059"}',
            login_state="online",
            status="online",
            is_enabled=True,
        ),
        Channel(
            bot_id=bot.id,
            name="OpenClaw 桥接",
            channel_type="openclaw",
            api_base="http://127.0.0.1:18789",
            bot_token_encrypted=encrypt_secret("openclaw-demo-token"),
            bot_token_masked=mask_secret("openclaw-demo-token"),
            webhook_path="/wechat_abot/webhook",
            dm_policy="allowlist",
            allow_from="wxid_xxx,wxid_yyy",
            adapter_config_json='{"provider":"openclaw","mode":"bridge"}',
            status="online",
            is_enabled=True,
        ),
        Channel(
            bot_id=bot.id,
            name="内置实验渠道",
            channel_type="simulator",
            api_base="simulator://local",
            webhook_path="/simulator/inbound",
            dm_policy="allow",
            allow_from="*",
            adapter_config_json='{"provider":"simulator","mode":"local-loopback"}',
            status="online",
            is_enabled=True,
        ),
    ]
    db.add_all(channels)
    db.flush()

    plugins = db.scalars(select(Plugin).order_by(Plugin.id.asc())).all()
    if not plugins:
        raise AssertionError("历史测试夹具未同步到任何插件目录项")
    # 测试为当前实际存在的每个目录插件建立绑定，状态严格沿用 manifest 默认值；
    # 这样运行时测试可以按需启用目标插件，同时不会伪造已经从仓库删除的插件。
    db.add_all(
        [
            BotPlugin(
                bot_id=bot.id,
                plugin_id=plugin.id,
                status="enabled" if plugin.default_enabled else "disabled",
            )
            for plugin in plugins
        ]
    )

    conversation = Conversation(
        bot_id=bot.id,
        channel_id=channels[0].id,
        channel_type="wechat_ipad",
        external_id="room_001",
        title="演示群聊",
        chat_type="group",
    )
    db.add(conversation)
    db.flush()
    db.add_all(
        [
            Message(
                conversation_id=conversation.id,
                direction="in",
                sender_id="wxid_demo_1",
                sender_name="张三",
                content="/总结 昨天群里说了什么？",
                message_type="text",
                status="received",
            ),
            Message(
                conversation_id=conversation.id,
                direction="out",
                sender_id="abot-main",
                sender_name="ABOT",
                content="已开始总结最近消息，请稍等。",
                message_type="text",
                status="sent",
            ),
        ]
    )
    db.commit()


class MultiBotAdminTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        self.client.cookies.clear()
        self._assert_test_database()
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        # init_db 使用进程级哨兵避免生产环境重复初始化；测试每次重建数据库后必须同步重置，
        # 否则第二个用例开始便会跳过用户、插件和运行时结构初始化，造成无关的级联失败。
        init_db_service._initialized = False
        with SessionLocal() as db:
            init_db_with_seed(db)
            seed_legacy_test_fixture(db)
            for username, password, role in (
                ("admin", "admin123", "admin"),
                ("ops", "ops123", "ops"),
                ("viewer", "viewer123", "readonly"),
            ):
                user = db.scalar(select(AdminUser).where(AdminUser.username == username))
                if user is None:
                    user = AdminUser(
                        username=username,
                        display_name=username,
                        password_hash=hash_password(password),
                        role=role,
                        is_enabled=True,
                        auth_version=1,
                    )
                    db.add(user)
                    continue
                user.password_hash = hash_password(password)
                user.role = role
                user.is_enabled = True
                user.auth_version = 1
            db.commit()

    def _assert_test_database(self):
        database_path = str(getattr(engine.url, "database", "") or "")
        # 这组测试会重建全部表，必须强制限定在 .run/test-runtime 下，避免误连真实开发库。
        self.assertIn(
            str(TEMP_ROOT),
            str(Path(database_path).resolve()),
            f"拒绝在非测试数据库上执行清表: {engine.url}",
        )

    def login(self, username="admin", password="admin123"):
        response = self.client.post(
            "/login",
            data={"username": username, "password": password},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        return response

    @staticmethod
    def _json_response(payload, url="http://example.test/final"):
        class FakeResponse:
            def __init__(self, payload, url):
                self.payload = payload
                self.url = url

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return FakeResponse(payload, url)

    @staticmethod
    def _fake_weather_snapshot():
        """为通用调度测试提供当前天气插件认可的最小业务结果。"""

        return {
            "city_id": "101020100",
            "city_name": "上海",
            "adm1": "上海",
            "country": "中国",
            "forecast_date": "2026-08-31",
            "condition_day": "多云",
            "condition_night": "小雨",
            "temp_min": 22,
            "temp_max": 30,
            "humidity": 61,
            "precipitation": 1.2,
            "uv_index": 5,
            "sunrise": "05:30",
            "sunset": "18:20",
            "wind_direction": "东南风",
            "wind_scale": "3-4",
        }

    @contextmanager
    def override_settings(self, **overrides):
        originals = {name: getattr(settings, name) for name in overrides}
        try:
            for name, value in overrides.items():
                setattr(settings, name, value)
            yield
        finally:
            for name, value in originals.items():
                setattr(settings, name, value)

    def build_temp_session(self):
        temp_dir = tempfile.mkdtemp(prefix="fulcrum-auth-")
        engine = create_engine(f"sqlite:///{temp_dir}/auth.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        return sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    def recreate_legacy_admin_users_table_without_auth_version(self):
        with SessionLocal() as db:
            db.execute(text("DROP TABLE IF EXISTS admin_users"))
            db.execute(
                text(
                    """
                    CREATE TABLE admin_users (
                        id INTEGER PRIMARY KEY,
                        username VARCHAR(50) UNIQUE,
                        display_name VARCHAR(100) NOT NULL,
                        password_hash VARCHAR(255) NOT NULL,
                        role VARCHAR(20),
                        is_enabled BOOLEAN,
                        created_at DATETIME,
                        updated_at DATETIME
                    )
                    """
                )
            )
            db.commit()

    def test_minimal_bot_create_and_edit_flow(self):
        self.login()
        response = self.client.post(
            "/bots",
            data={"name": "ABOT-客服"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("/bots/", response.headers["location"])

        with SessionLocal() as db:
            created = db.scalar(select(Bot).where(Bot.name == "ABOT-客服"))
            self.assertIsNotNone(created)
            created_id = created.id

        update = self.client.post(
            f"/bots/{created_id}/edit",
            data={"name": "ABOT-售后", "description": "售后专用机器人"},
            follow_redirects=False,
        )
        self.assertEqual(update.status_code, 303)

        page = self.client.get("/bots")
        self.assertEqual(page.status_code, 200)
        self.assertIn("ABOT-售后", page.text)
        detail = self.client.get(f"/bots/{created_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertIn("售后专用机器人", detail.text)

    def test_webhook_signature_and_outbound_flow(self):
        self.login("ops", "ops123")
        # 历史夹具保留了旧渠道地址，但回归测试必须完全阻断真实局域网探测。
        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter.health_check",
            return_value={
                "ok": True,
                "provider": "wechat_ipad",
                "status": "online",
                "summary": "测试心跳正常",
                "details": {"probes": []},
            },
        ):
            self.client.post("/channels/1/health-check", follow_redirects=False)
        self.client.post("/bots/1/status-check", follow_redirects=False)

        payload = {
            "Event": "AddMsg",
            "Wxid": "wxid_demo_account_001",
            "Data": {
                "FromUserName": {"string": "room-sign@chatroom"},
                "ToUserName": {"string": "wxid_demo_account_001"},
                "Content": {"string": "wxid_sender:\nhello"},
                "MsgType": 1,
                "MsgId": "evt-001",
            },
        }
        body = json.dumps(payload).encode("utf-8")
        timestamp = "171111"
        signature = sign_webhook_payload("wx-demo-token-main", body, timestamp)

        inbound_ok = self.client.post(
            "/webhooks/1/inbound",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Timestamp": timestamp,
                "X-Signature": f"sha256={signature}",
            },
        )
        self.assertEqual(inbound_ok.status_code, 200)
        self.assertTrue(inbound_ok.json()["ok"])
        self.assertEqual(inbound_ok.json()["event_type"], "AddMsg")

        inbound_bad = self.client.post(
            "/webhooks/1/inbound",
            content=body,
            headers={"Content-Type": "application/json", "X-Timestamp": timestamp, "X-Signature": "sha256=bad"},
        )
        self.assertEqual(inbound_bad.status_code, 401)
        self.assertEqual(inbound_bad.json()["error"], "invalid_signature")

        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._post_json",
            return_value={"Ret": 0, "Msg": "OK", "MsgId": "msg-170", "http_status": 200},
        ):
            outbound = self.client.post(
                "/channels/1/outbound",
                json={"conversation_id": 1, "content": "测试真实出站", "message_type": "text"},
            )
            self.assertEqual(outbound.status_code, 200)
            outbound_data = outbound.json()
            self.assertTrue(outbound_data["queued"])

            # 当前出站接口只负责可靠入队；Worker 会先处理较高优先级的入站插件任务，
            # 因此按任务真实状态推进，而不是假定固定调用次数或恢复旧同步发送语义。
            for _ in range(3):
                with SessionLocal() as db:
                    outbound_job = db.get(TaskJob, outbound_data["job_id"])
                    self.assertIsNotNone(outbound_job)
                    if outbound_job.status == "completed":
                        break
                self.assertTrue(async_workers._run_single_job())

        with SessionLocal() as db:
            outbound_job = db.get(TaskJob, outbound_data["job_id"])
            self.assertIsNotNone(outbound_job)
            self.assertEqual(outbound_job.status, "completed")
            worker_result = json.loads(outbound_job.result_json or "{}")
            adapter_result = worker_result["adapter_result"]
            self.assertEqual(adapter_result["transport_status"], "sent")
            self.assertEqual(adapter_result["provider"], "wechat_ipad")
            self.assertEqual(adapter_result["external_message_id"], "msg-170")
            message = db.scalar(select(Message).where(Message.id == worker_result["message_id"]))
            self.assertIsNotNone(message)
            self.assertEqual(message.status, "sent")
            self.assertEqual(message.external_status, "0")
            self.assertEqual(message.external_message_id, "msg-170")
            self.assertIn("/api/Msg/SendTxt", message.raw_request_json or "")
            self.assertIn("\"MsgId\": \"msg-170\"", message.raw_response_json or "")

            inbound_message = db.scalar(select(Message).where(Message.direction == "in").order_by(desc(Message.id)))
            self.assertIsNotNone(inbound_message)
            self.assertEqual(inbound_message.sender_id, "wxid_sender")
            self.assertEqual(inbound_message.content, "hello")
            self.assertEqual(inbound_message.message_type, "text")
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            self.assertIn("last_receive_summary", channel.runtime_info_json or "")
            self.assertIn("\"receive_total\": 1", channel.runtime_info_json or "")
            self.assertIn("last_send_summary", channel.runtime_info_json or "")

    def test_generic_channel_outbound_uses_real_http_adapter(self):
        self.login("ops", "ops123")

        class FakeResponse:
            def __init__(self, payload, url):
                self.payload = payload
                self.url = url
                self.status = 200

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=0):
            self.assertEqual(req.get_method(), "POST")
            self.assertEqual(req.full_url, "http://127.0.0.1:18789/wechat_abot/webhook")
            body = json.loads(req.data.decode("utf-8"))
            self.assertEqual(body["message_type"], "text")
            self.assertEqual(body["content"], "桥接通道真实出站")
            self.assertEqual(body["receiver_id"], "openclaw-room-001")
            self.assertEqual(req.headers.get("Authorization"), "Bearer openclaw-demo-token")
            return FakeResponse({"ok": True, "status": "accepted", "message_id": "bridge-msg-001"}, req.full_url)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            outbound = self.client.post(
                "/channels/2/outbound",
                json={
                    "external_id": "openclaw-room-001",
                    "content": "桥接通道真实出站",
                    "message_type": "text",
                    "chat_type": "group",
                },
            )

        self.assertEqual(outbound.status_code, 200)
        outbound_data = outbound.json()
        self.assertTrue(outbound_data["ok"])
        self.assertEqual(outbound_data["adapter"]["provider"], "generic_http")
        self.assertEqual(outbound_data["adapter"]["transport_status"], "sent")
        self.assertEqual(outbound_data["adapter"]["external_message_id"], "bridge-msg-001")
        self.assertEqual(outbound_data["adapter"]["endpoint"], "/wechat_abot/webhook")

        with SessionLocal() as db:
            message = db.scalar(select(Message).where(Message.id == outbound_data["message_id"]))
            self.assertIsNotNone(message)
            self.assertEqual(message.status, "sent")
            self.assertEqual(message.external_status, "accepted")
            self.assertEqual(message.external_message_id, "bridge-msg-001")
            self.assertIn("/wechat_abot/webhook", message.raw_request_json or "")
            self.assertIn("\"message_id\": \"bridge-msg-001\"", message.raw_response_json or "")

    def test_telegram_channel_outbound_inbound_and_health_check(self):
        self.login("ops", "ops123")
        response = self.client.post(
            "/channels",
            data={
                "bot_id": "1",
                "name": "Telegram Bot",
                "channel_type": "telegram",
                "api_base": "https://api.telegram.org",
                "bot_token": "123456:telegram-test-token",
                "webhook_path": "/webhooks/4/inbound",
                "dm_policy": "allow",
                "allow_from": "*",
                "adapter_config_json": "{}",
                "is_enabled": "true",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        class FakeResponse:
            def __init__(self, payload, url):
                self.payload = payload
                self.url = url
                self.status = 200

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_telegram_urlopen(req, timeout=0):
            url = req.full_url
            body = json.loads(req.data.decode("utf-8")) if getattr(req, "data", None) else {}
            if url.endswith("/sendMessage"):
                self.assertIn("/bot123456:telegram-test-token/sendMessage", url)
                self.assertEqual(body["chat_id"], "-1001234567890")
                self.assertEqual(body["text"], "你好，Telegram")
                return FakeResponse(
                    {"ok": True, "result": {"message_id": 777, "chat": {"id": -1001234567890}}},
                    url,
                )
            if url.endswith("/getMe"):
                return FakeResponse(
                    {
                        "ok": True,
                        "result": {
                            "id": 123456,
                            "is_bot": True,
                            "first_name": "Fulcrum",
                            "username": "liu_mba_bot",
                            "can_join_groups": True,
                            "supports_inline_queries": False,
                        },
                    },
                    url,
                )
            if url.endswith("/getWebhookInfo"):
                return FakeResponse(
                    {
                        "ok": True,
                        "result": {
                            "url": "https://example.com/webhooks/4/inbound",
                            "pending_update_count": 0,
                        },
                    },
                    url,
                )
            raise AssertionError(f"unexpected telegram url {url}")

        with patch("urllib.request.urlopen", side_effect=fake_telegram_urlopen):
            with SessionLocal() as db:
                from app.services.channel_adapters.service import dispatch_outbound_message

                channel = db.get(Channel, 4)
                self.assertIsNotNone(channel)
                conversation = Conversation(
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    channel_type=channel.channel_type,
                    external_id="-1001234567890",
                    title="Telegram Group",
                    chat_type="group",
                )
                db.add(conversation)
                db.flush()

                message, adapter_result = dispatch_outbound_message(
                    db,
                    channel,
                    conversation,
                    {"external_id": "-1001234567890", "content": "你好，Telegram", "message_type": "text"},
                )
                self.assertTrue(adapter_result["ok"])
                self.assertEqual(adapter_result["provider"], "telegram")
                self.assertEqual(adapter_result["external_message_id"], "777")
                self.assertEqual(message.status, "sent")
                self.assertEqual(message.external_status, "ok")
                self.assertNotIn("123456:telegram-test-token", message.raw_request_json or "")
                self.assertIn("sendMessage", message.raw_request_json or "")

            health = self.client.post("/channels/4/health-check", follow_redirects=False)
            self.assertEqual(health.status_code, 303)

        inbound_payload = {
            "update_id": 9990001,
            "message": {
                "message_id": 8801,
                "date": 1711111111,
                "text": "/start",
                "chat": {
                    "id": 123456789,
                    "type": "private",
                    "first_name": "Liu",
                    "username": "liutest",
                },
                "from": {
                    "id": 123456789,
                    "is_bot": False,
                    "first_name": "Liu",
                    "username": "liutest",
                },
            },
        }
        secret = build_telegram_webhook_secret("123456:telegram-test-token", 4)
        inbound = self.client.post(
            "/webhooks/4/inbound",
            json=inbound_payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        )
        self.assertEqual(inbound.status_code, 200)
        self.assertTrue(inbound.json()["ok"])
        self.assertEqual(inbound.json()["event_type"], "message")

        inbound_bad = self.client.post(
            "/webhooks/4/inbound",
            json=inbound_payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "bad-secret"},
        )
        self.assertEqual(inbound_bad.status_code, 401)

        with SessionLocal() as db:
            channel = db.get(Channel, 4)
            self.assertIsNotNone(channel)
            self.assertEqual(channel.status, "online")
            conversation = db.scalar(
                select(Conversation).where(
                    Conversation.channel_id == 4,
                    Conversation.external_id == "123456789",
                )
            )
            self.assertIsNotNone(conversation)
            self.assertEqual(conversation.chat_type, "direct")
            inbound_message = db.scalar(
                select(Message)
                .where(Message.conversation_id == conversation.id, Message.direction == "in")
                .order_by(desc(Message.id))
            )
            self.assertIsNotNone(inbound_message)
            self.assertEqual(inbound_message.content, "/start")
            self.assertEqual(inbound_message.sender_id, "123456789")
            self.assertEqual(inbound_message.message_type, "text")

    def test_wechat_outbound_media_mapping_and_health_check(self):
        self.login("ops", "ops123")
        image_xml = "<msg><img aeskey='k1' cdnmidimgurl='https://cdn.example.com/a.png' /></msg>"
        file_xml = "<appmsg><title>报价单.pdf</title><type>74</type></appmsg>"

        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._post_json",
            return_value={"Ret": 0, "Msg": "OK", "MsgId": "img-170", "http_status": 200},
        ):
            image_response = self.client.post(
                "/channels/1/outbound",
                json={"external_id": "wxid_target", "message_type": "image", "content": image_xml},
            )
        self.assertEqual(image_response.status_code, 200)
        image_data = image_response.json()
        self.assertEqual(image_data["adapter"]["endpoint"], IPAD_874_CONTRACT.endpoints.message_send_cdn_image)

        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._post_json",
            return_value={"Ret": 0, "Msg": "OK", "MsgId": "voice-170", "http_status": 200},
        ):
            voice_response = self.client.post(
                "/channels/1/outbound",
                json={
                    "external_id": "wxid_target",
                    "message_type": "voice",
                    "base64": "YWJj",
                    "voice_time": 1800,
                    "voice_type": 0,
                },
            )
        self.assertEqual(voice_response.status_code, 200)
        voice_data = voice_response.json()
        self.assertEqual(voice_data["adapter"]["endpoint"], IPAD_874_CONTRACT.endpoints.message_send_voice)

        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._encode_audio_to_silk",
            return_value={"bytes": b"silk-bytes", "duration_ms": 2400, "sample_rate": 24000},
        ), patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._post_json",
            return_value={"Ret": 0, "Msg": "OK", "MsgId": "voice-171", "http_status": 200},
        ):
            converted_voice_response = self.client.post(
                "/channels/1/outbound",
                json={
                    "external_id": "wxid_target",
                    "message_type": "voice",
                    "base64": "d2F2LWJ5dGVz",
                    "format": "wav",
                },
            )
        self.assertEqual(converted_voice_response.status_code, 200)
        converted_voice_data = converted_voice_response.json()
        self.assertEqual(converted_voice_data["adapter"]["endpoint"], IPAD_874_CONTRACT.endpoints.message_send_voice)

        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._post_json",
            return_value={"Ret": 0, "Msg": "OK", "MsgId": "video-170", "http_status": 200},
        ):
            video_response = self.client.post(
                "/channels/1/outbound",
                json={
                    "external_id": "wxid_target",
                    "message_type": "video",
                    "base64": "dmlkZW8=",
                    "image_base64": "aW1hZ2U=",
                    "play_length": 6,
                },
            )
        self.assertEqual(video_response.status_code, 200)
        video_data = video_response.json()
        self.assertEqual(video_data["adapter"]["endpoint"], IPAD_874_CONTRACT.endpoints.message_send_video)

        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._post_json",
            return_value={"Ret": 0, "Msg": "OK", "MsgId": "file-170", "http_status": 200},
        ):
            file_response = self.client.post(
                "/channels/1/outbound",
                json={
                    "external_id": "wxid_target",
                    "message_type": "file",
                    "xml": file_xml,
                },
            )
        self.assertEqual(file_response.status_code, 200)
        file_data = file_response.json()
        self.assertEqual(file_data["adapter"]["endpoint"], IPAD_874_CONTRACT.endpoints.message_send_app)

        response = self.client.post(
            "/channels/1/outbound",
            json={"external_id": "wxid_target", "message_type": "file"},
        )
        self.assertEqual(response.status_code, 502)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["adapter"]["error_code"], "invalid_payload")
        self.assertIn("file_name + file_url/download_url", data["adapter"]["error_message"])

        with patch(
            "app.services.channel_adapters.wechat_ipad.WechatIpadAdapter._run_health_probe",
            side_effect=[
                {
                    "ok": True,
                    "endpoint": IPAD_874_CONTRACT.endpoints.login_heartbeat,
                    "http_status": 200,
                    "response_status": "0",
                    "error_message": None,
                },
                {
                    "ok": False,
                    "endpoint": "/api/Login/GetCacheInfo",
                    "http_status": 500,
                    "response_status": "500",
                    "error_message": "cache not ready",
                },
            ],
        ) as health_probe:
            with SessionLocal() as db:
                channel = db.get(Channel, 1)
                self.assertIsNotNone(channel)
                config = json.loads(channel.adapter_config_json or "{}")
                config["wxid"] = "wxid_demo_account_001/slash"
                channel.adapter_config_json = json.dumps(config, ensure_ascii=False)
                db.commit()
            health_redirect = self.client.post("/channels/1/health-check", follow_redirects=False)
        self.assertEqual(health_redirect.status_code, 303)
        self.assertEqual(
            health_probe.call_args_list[0].args[1],
            IPAD_874_CONTRACT.endpoints.login_heartbeat,
        )
        self.assertEqual(
            health_probe.call_args_list[0].kwargs["query"],
            {"wxid": "wxid_demo_account_001/slash"},
        )

        with SessionLocal() as db:
            message = db.scalar(select(Message).where(Message.id == data["message_id"]))
            self.assertIsNotNone(message)
            self.assertEqual(message.status, "failed")
            self.assertEqual(message.external_status, "failed")
            file_message = db.scalar(select(Message).where(Message.id == file_data["message_id"]))
            self.assertIsNotNone(file_message)
            self.assertIn("<appmsg", file_message.raw_request_json or "")
            image_message = db.scalar(select(Message).where(Message.id == image_data["message_id"]))
            self.assertIsNotNone(image_message)
            self.assertIn("/api/Msg/SendCDNImg", image_message.raw_request_json or "")
            self.assertIn("\"Content\":", image_message.raw_request_json or "")
            voice_message = db.scalar(select(Message).where(Message.id == voice_data["message_id"]))
            self.assertIsNotNone(voice_message)
            self.assertIn("/api/Msg/SendVoice", voice_message.raw_request_json or "")
            self.assertIn("\"VoiceTime\": 1800", voice_message.raw_request_json or "")
            converted_voice_message = db.scalar(select(Message).where(Message.id == converted_voice_data["message_id"]))
            self.assertIsNotNone(converted_voice_message)
            self.assertIn("\"Type\": 4", converted_voice_message.raw_request_json or "")
            self.assertIn("\"VoiceTime\": 2400", converted_voice_message.raw_request_json or "")
            video_message = db.scalar(select(Message).where(Message.id == video_data["message_id"]))
            self.assertIsNotNone(video_message)
            self.assertIn("/api/Msg/SendVideo", video_message.raw_request_json or "")
            self.assertIn("data:video/mp4;base64,", video_message.raw_request_json or "")
            self.assertIn("data:image/jpeg;base64,", video_message.raw_request_json or "")
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            self.assertEqual(channel.status, "degraded")
            self.assertIn("wxid_demo_account_001/slash", channel.adapter_config_json or "")
            self.assertIn("last_send_summary", channel.runtime_info_json or "")
            self.assertIn("\"Type\": 74", file_message.raw_request_json or "")
            latest_health = db.scalar(
                select(HealthCheckRecord)
                .where(HealthCheckRecord.target_type == "channel", HealthCheckRecord.target_id == 1)
                .order_by(desc(HealthCheckRecord.id))
            )
            self.assertIsNotNone(latest_health)
            self.assertEqual(latest_health.status, "degraded")
            self.assertIn("缓存探针异常", latest_health.summary)

    def test_plugin_config_schema_registry_returns_registered_schema(self):
        schema = get_plugin_config_schema("dify")
        self.assertIsNotNone(schema)
        self.assertEqual(schema.plugin_code, "dify")
        self.assertEqual(schema.schema_version, "v1")
        field_names = [item.name for item in schema.fields]
        self.assertIn("api_key", field_names)
        self.assertIn("commands", field_names)
        self.assertTrue(any(item.sensitive for item in schema.fields))

    def test_new_plugin_registry_discovers_native_and_bridge_plugins(self):
        definitions = {item.plugin_code: item for item in list_plugin_runtime_definitions()}
        self.assertIn("weather", definitions)
        self.assertIn("message_summary", definitions)
        self.assertIn("dify", definitions)
        self.assertIn("guess_song", definitions)
        self.assertIn("douyu", definitions)
        self.assertEqual(definitions["weather"].architecture_source, "plugin_runtime")
        self.assertTrue(definitions["weather"].supports_execution)
        self.assertEqual(definitions["message_summary"].architecture_source, "plugin_runtime")
        self.assertEqual(definitions["dify"].architecture_source, "plugin_runtime")
        self.assertEqual(definitions["guess_song"].migration_state, "migrated")
        self.assertEqual(definitions["douyu"].migration_state, "migrated")
        self.assertTrue(definitions["guess_song"].supports_execution)
        self.assertTrue(definitions["douyu"].supports_execution)
        self.assertEqual(definitions["dify"].plugin_category, "functional")
        self.assertIsNotNone(get_plugin_runtime_definition("weather"))
        runtime_info = get_plugin_runtime_meta("message_summary")
        self.assertEqual(runtime_info["architecture_source"], "plugin_runtime")
        self.assertTrue(runtime_info["supports_execution"])

    def test_directory_plugin_scanner_discovers_config_toml_and_entrypoint_variants(self):
        discovered = {item.manifest.code: item for item in discover_plugins(force=True)}
        self.assertIn("dify", discovered)
        self.assertIn("message_summary", discovered)
        self.assertIn("weather", discovered)
        self.assertEqual(discovered["dify"].entrypoint_name, "plugin.py")
        self.assertEqual(discovered["message_summary"].entrypoint_name, "plugin.py")
        self.assertEqual(discovered["weather"].entrypoint_name, "plugin.py")
        self.assertEqual(
            discovered["dify"].capability.channel_types,
            ("wechat_ipad", "wechat_winauto", "telegram", "openclaw", "simulator", "workwechat"),
        )
        self.assertEqual(discovered["message_summary"].matcher.commands, ("总结", "summary"))

        runtime_info = get_plugin_runtime_meta("dify")
        self.assertEqual(runtime_info["catalog_source"], "directory")
        self.assertEqual(runtime_info["commands"], ["聊天"])
        self.assertTrue(str(runtime_info["config_path"]).endswith("plugins/dify/config.toml"))

        loaded = load_directory_plugin("message_summary", force=True)
        self.assertIsNotNone(loaded)
        self.assertFalse(loaded.supports_handler)
        self.assertTrue(loaded.supports_matcher)
        self.assertTrue(hasattr(loaded.instance, "execute_message_action"))

    def test_runtime_only_plugins_do_not_claim_directory_handler_support(self):
        for plugin_code in ("weather", "dify", "guess_song", "douyu"):
            loaded = load_directory_plugin(plugin_code, force=True)
            self.assertIsNotNone(loaded)
            self.assertIsNotNone(loaded.instance)
            self.assertFalse(loaded.supports_handler, plugin_code)
            self.assertTrue(hasattr(loaded.instance, "execute_action"), plugin_code)

    def test_directory_plugin_scanner_collects_manifest_validation_errors(self):
        with tempfile.TemporaryDirectory(prefix="fulcrum-plugins-", dir=TEMP_ROOT) as plugin_root:
            plugin_root_path = Path(plugin_root)
            missing_plugin_dir = plugin_root_path / "missing_plugin"
            missing_plugin_dir.mkdir(parents=True, exist_ok=True)
            missing_plugin_dir.joinpath("config.toml").write_text(
                """
[capability]
channel_types = ["wechat_ipad"]
""".strip(),
                encoding="utf-8",
            )
            missing_plugin_dir.joinpath("plugin.py").write_text("plugin = object()\n", encoding="utf-8")

            invalid_manifest_dir = plugin_root_path / "bad_manifest"
            invalid_manifest_dir.mkdir(parents=True, exist_ok=True)
            invalid_manifest_dir.joinpath("config.toml").write_text(
                """
[plugin]
code = "other_code"
display_name = "坏插件"
plugin_type = "alien"

[capability]
channel_types = 123
consume_policy = "shared"
timeout_ms = -1
priority = "oops"

[matcher]
commands = 123
""".strip(),
                encoding="utf-8",
            )
            invalid_manifest_dir.joinpath("plugin.py").write_text("plugin = object()\n", encoding="utf-8")

            with self.override_settings(plugins_dir=str(plugin_root_path)):
                discovered = {item.manifest.code: item for item in discover_plugins(force=True)}

        self.assertIn("missing_plugin", discovered)
        self.assertIn("bad_manifest", discovered)
        self.assertFalse(discovered["missing_plugin"].is_valid)
        self.assertIn("缺少 [plugin] 配置块。", discovered["missing_plugin"].validation_errors)
        self.assertIn("缺少 plugin.code。", discovered["missing_plugin"].validation_errors)

        bad_manifest = discovered["bad_manifest"]
        self.assertFalse(bad_manifest.is_valid)
        self.assertEqual(bad_manifest.manifest.code, "bad_manifest")
        self.assertIn("plugin.code 与目录名不一致：声明为 other_code，目录为 bad_manifest。", bad_manifest.validation_errors)
        self.assertIn("plugin_type 非法：alien。", bad_manifest.validation_errors)
        self.assertIn("consume_policy 非法：shared。", bad_manifest.validation_errors)
        self.assertIn("timeout_ms 不能小于 0。", bad_manifest.validation_errors)
        self.assertIn("priority 必须是整数。", bad_manifest.validation_errors)
        self.assertIn("channel_types 必须是字符串或字符串数组。", bad_manifest.validation_errors)
        self.assertIn("commands 必须是字符串或字符串数组。", bad_manifest.validation_errors)
        self.assertTrue(any("当前按目录名 bad_manifest 作为扫描镜像编码" in item for item in bad_manifest.validation_warnings))

    def test_sync_plugins_from_directory_keeps_invalid_manifest_as_observable_mirror(self):
        with tempfile.TemporaryDirectory(prefix="fulcrum-plugins-", dir=TEMP_ROOT) as plugin_root:
            plugin_root_path = Path(plugin_root)
            broken_dir = plugin_root_path / "broken_plugin"
            broken_dir.mkdir(parents=True, exist_ok=True)
            broken_dir.joinpath("config.toml").write_text(
                """
[plugin]
display_name = "坏插件镜像"
plugin_type = "message"

[capability]
consume_policy = "continue"
""".strip(),
                encoding="utf-8",
            )
            broken_dir.joinpath("plugin.py").write_text("plugin = object()\n", encoding="utf-8")

            with self.override_settings(plugins_dir=str(plugin_root_path)):
                with SessionLocal() as db:
                    rows = sync_plugins_from_directory(db, force=True)
                    synced = {item.code: item for item in rows}
                    runtime_info = get_plugin_runtime_meta("broken_plugin")

        self.assertIn("broken_plugin", synced)
        self.assertEqual(synced["broken_plugin"].plugin_type, "invalid")
        self.assertIn("manifest 校验失败", synced["broken_plugin"].description or "")
        self.assertFalse(runtime_info["manifest_valid"])
        self.assertIn("缺少 plugin.code。", runtime_info["manifest_errors"])

    def test_static_dispatch_plan_filters_disabled_and_non_message_plugins(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            plan = build_static_dispatch_plan(
                db,
                channel,
                {
                    "conversation": {"external_id": "room_dispatch", "chat_type": "group"},
                    "message": {"content": "/总结 昨天群里说了什么", "sender_id": "wxid_sender", "message_type": "text"},
                    "details": {"mentioned_self": False},
                },
            )

        self.assertEqual(plan.candidate_plugin_codes, ("dify", "message_summary"))
        skipped = dict(plan.skipped)
        self.assertEqual(skipped["weather"], "binding_disabled")

    def test_static_dispatch_plan_skips_inactive_wechat_chatroom(self):
        with SessionLocal() as db:
            # 插件调度使用机器人主键，精简模型不再需要独立 slug。
            bot = Bot(name="Dispatch Bot")
            db.add(bot)
            db.flush()
            plugin_codes = {"dify", "message_summary"}
            plugins = db.scalars(select(Plugin).where(Plugin.code.in_(plugin_codes))).all()
            if len(plugins) < 2:
                sync_plugins_from_directory(db, force=True)
                plugins = db.scalars(select(Plugin).where(Plugin.code.in_(plugin_codes))).all()
            self.assertEqual({item.code for item in plugins}, plugin_codes)
            for plugin in plugins:
                db.add(BotPlugin(bot_id=bot.id, plugin_id=plugin.id, status="enabled"))
            channel = Channel(
                bot_id=bot.id,
                name="Wechat iPad Gate",
                channel_type="wechat_ipad",
                webhook_path="/webhooks/test/wechat-ipad",
            )
            db.add(channel)
            db.flush()
            chatroom = Chatroom(
                bot_id=bot.id,
                channel_id=channel.id,
                external_id="room-discovered@chatroom",
                name="待启用群",
                registration_source="passive",
                management_status="discovered",
                is_registered=True,
                is_active=False,
            )
            db.add(chatroom)
            db.commit()

            plan = build_static_dispatch_plan(
                db,
                channel,
                {
                    "conversation": {"external_id": "room-discovered@chatroom", "chat_type": "group"},
                    "message": {"content": "/总结 今天群里发生了什么", "sender_id": "wxid_sender", "message_type": "text"},
                    "details": {"mentioned_self": False},
                },
            )

        self.assertEqual(plan.candidate_plugin_codes, ())
        skipped = dict(plan.skipped)
        self.assertEqual(skipped["dify"], "group_chatroom_inactive")
        self.assertEqual(skipped["message_summary"], "group_chatroom_inactive")

    def test_run_single_job_executes_claimed_plugin_dispatch_job(self):
        with SessionLocal() as db:
            job = enqueue_job(
                db,
                job_type="plugin_dispatch",
                payload={"channel_id": 1, "normalized_event": {"message": {"content": "/天气 长沙"}}},
                related_type="raw_webhook_event",
                related_id=999,
            )
            job_id = job.id
            db.commit()

        with patch("app.services.async_workers._process_plugin_dispatch_job", return_value={"ok": True}) as process_mock:
            worked = async_workers._run_single_job()

        self.assertTrue(worked)
        process_mock.assert_called_once()

        with SessionLocal() as db:
            persisted = db.get(TaskJob, job_id)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted.status, "completed")
            self.assertEqual(json.loads(persisted.result_json or "{}"), {"ok": True})

    def test_dynamic_dispatch_report_marks_sync_directory_handler_timeout(self):
        original_loader = load_directory_plugin
        original_discover_plugins = discover_plugins

        class SlowPlugin:
            def handle_event(self, context):
                time.sleep(0.05)
                return {"ok": True, "status": "completed", "message": "不应该执行到这里"}

        def fake_loader(plugin_code: str | None, *, force: bool = False):
            if plugin_code == "message_summary":
                discovered = original_discover_plugins(force=force)
                entrypoint = next(item.entrypoint_path for item in discovered if item.manifest.code == "message_summary")
                return LoadedDirectoryPlugin(
                    plugin_code="message_summary",
                    module_name="test_message_summary_slow_sync",
                    entrypoint_path=entrypoint,
                    instance=SlowPlugin(),
                    supports_matcher=False,
                    supports_handler=True,
                )
            return original_loader(plugin_code, force=force)

        def fake_discover_plugins(*, force: bool = False):
            modified = []
            for item in original_discover_plugins(force=force):
                if item.manifest.code == "message_summary":
                    modified.append(replace(item, capability=replace(item.capability, timeout_ms=10)))
                    continue
                modified.append(item)
            return tuple(modified)

        with patch("app.services.plugin_directory.load_directory_plugin", side_effect=fake_loader):
            with patch("app.services.plugin_directory.discover_plugins", side_effect=fake_discover_plugins):
                with SessionLocal() as db:
                    channel = db.get(Channel, 1)
                    self.assertIsNotNone(channel)
                    report = build_dispatch_report(
                        db,
                        channel,
                        {
                            "conversation": {"external_id": "room_dispatch_timeout_sync", "chat_type": "group"},
                            "message": {"content": "/总结 同步超时", "sender_id": "wxid_sender", "message_type": "text"},
                            "details": {"mentioned_self": False},
                        },
                        raw_event_id=131,
                        conversation_id=231,
                        message_id=331,
                    )

        execution = next(item for item in report.executions if item.candidate.plugin_code == "message_summary")
        self.assertEqual(execution.status, "timeout")
        self.assertFalse(execution.ok)
        self.assertTrue(execution.debug["timeout_enforced"])
        self.assertEqual(execution.debug["timeout_ms"], 10)
        self.assertEqual(execution.debug["execution_source"], "directory_handler_timeout")

    def test_dynamic_dispatch_report_marks_async_directory_handler_timeout(self):
        original_loader = load_directory_plugin
        original_discover_plugins = discover_plugins

        class SlowAsyncPlugin:
            async def handle_event(self, context):
                await asyncio.sleep(0.05)
                return {"ok": True, "status": "completed", "message": "不应该执行到这里"}

        def fake_loader(plugin_code: str | None, *, force: bool = False):
            if plugin_code == "message_summary":
                discovered = original_discover_plugins(force=force)
                entrypoint = next(item.entrypoint_path for item in discovered if item.manifest.code == "message_summary")
                return LoadedDirectoryPlugin(
                    plugin_code="message_summary",
                    module_name="test_message_summary_slow_async",
                    entrypoint_path=entrypoint,
                    instance=SlowAsyncPlugin(),
                    supports_matcher=False,
                    supports_handler=True,
                )
            return original_loader(plugin_code, force=force)

        def fake_discover_plugins(*, force: bool = False):
            modified = []
            for item in original_discover_plugins(force=force):
                if item.manifest.code == "message_summary":
                    modified.append(replace(item, capability=replace(item.capability, timeout_ms=10)))
                    continue
                modified.append(item)
            return tuple(modified)

        with patch("app.services.plugin_directory.load_directory_plugin", side_effect=fake_loader):
            with patch("app.services.plugin_directory.discover_plugins", side_effect=fake_discover_plugins):
                with SessionLocal() as db:
                    channel = db.get(Channel, 1)
                    self.assertIsNotNone(channel)
                    report = build_dispatch_report(
                        db,
                        channel,
                        {
                            "conversation": {"external_id": "room_dispatch_timeout_async", "chat_type": "group"},
                            "message": {"content": "/总结 异步超时", "sender_id": "wxid_sender", "message_type": "text"},
                            "details": {"mentioned_self": False},
                        },
                        raw_event_id=132,
                        conversation_id=232,
                        message_id=332,
                    )

        execution = next(item for item in report.executions if item.candidate.plugin_code == "message_summary")
        self.assertEqual(execution.status, "timeout")
        self.assertFalse(execution.ok)
        self.assertTrue(execution.debug["timeout_enforced"])
        self.assertEqual(execution.debug["timeout_ms"], 10)
        self.assertEqual(execution.debug["execution_source"], "directory_handler_timeout")

    def test_dispatch_priority_affects_execution_order(self):
        original_discover_plugins = discover_plugins

        def fake_discover_plugins(*, force: bool = False):
            modified = []
            for item in original_discover_plugins(force=force):
                if item.manifest.code == "weather":
                    modified.append(
                        replace(
                            item,
                            capability=replace(item.capability, priority=130),
                            matcher=replace(item.matcher, commands=("总结",)),
                        )
                    )
                    continue
                modified.append(item)
            return tuple(modified)

        with patch("app.services.plugin_directory.discover_plugins", side_effect=fake_discover_plugins):
            with SessionLocal() as db:
                plugin = db.scalar(select(Plugin).where(Plugin.code == "weather"))
                self.assertIsNotNone(plugin)
                binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
                self.assertIsNotNone(binding)
                binding.status = "enabled"
                binding.config_json = json.dumps(
                    {
                        "commands": ["总结"],
                        "api_base": "https://weather.example.test",
                        "default_city": "香港",
                    }
                )
                db.commit()

                channel = db.get(Channel, 1)
                self.assertIsNotNone(channel)
                loaded_weather = load_directory_plugin("weather", force=True)
                self.assertIsNotNone(loaded_weather)
                weather_module = loaded_weather.instance._module
                with patch.object(weather_module, "_validate_runtime_config", return_value=None), patch.object(
                    weather_module,
                    "_fetch_weather_bundle",
                    return_value=self._fake_weather_snapshot(),
                ):
                    report = build_dispatch_report(
                        db,
                        channel,
                        {
                            "conversation": {"external_id": "room_dispatch_priority", "chat_type": "group"},
                            "message": {"content": "/总结 看下顺序", "sender_id": "wxid_sender", "message_type": "text"},
                            "details": {"mentioned_self": False},
                        },
                        raw_event_id=121,
                        conversation_id=221,
                        message_id=321,
                    )

        self.assertEqual(report.static_plan.candidate_plugin_codes, ("weather", "dify", "message_summary"))
        attempted_codes = [item.candidate.plugin_code for item in report.executions if item.attempted]
        self.assertEqual(attempted_codes, ["weather", "message_summary"])
        execution_map = {item.candidate.plugin_code: item for item in report.executions}
        self.assertEqual(execution_map["weather"].debug["execution_source"], "runtime_executor_executed")
        self.assertEqual(execution_map["message_summary"].debug["execution_source"], "directory_handler_executed")

    def test_dispatch_consume_policy_can_stop_following_plugins(self):
        original_discover_plugins = discover_plugins

        def fake_discover_plugins(*, force: bool = False):
            modified = []
            for item in original_discover_plugins(force=force):
                if item.manifest.code == "weather":
                    modified.append(
                        replace(
                            item,
                            capability=replace(item.capability, priority=130, consume_policy="consume"),
                            matcher=replace(item.matcher, commands=("总结",)),
                        )
                    )
                    continue
                modified.append(item)
            return tuple(modified)

        with patch("app.services.plugin_directory.discover_plugins", side_effect=fake_discover_plugins):
            with SessionLocal() as db:
                plugin = db.scalar(select(Plugin).where(Plugin.code == "weather"))
                self.assertIsNotNone(plugin)
                binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
                self.assertIsNotNone(binding)
                binding.status = "enabled"
                binding.config_json = json.dumps(
                    {
                        "commands": ["总结"],
                        "api_base": "https://weather.example.test",
                        "default_city": "香港",
                    }
                )
                db.commit()

                channel = db.get(Channel, 1)
                self.assertIsNotNone(channel)
                loaded_weather = load_directory_plugin("weather", force=True)
                self.assertIsNotNone(loaded_weather)
                weather_module = loaded_weather.instance._module
                with patch.object(weather_module, "_validate_runtime_config", return_value=None), patch.object(
                    weather_module,
                    "_fetch_weather_bundle",
                    return_value=self._fake_weather_snapshot(),
                ):
                    report = build_dispatch_report(
                        db,
                        channel,
                        {
                            "conversation": {"external_id": "room_dispatch_consume", "chat_type": "group"},
                            "message": {"content": "/总结 截断后续", "sender_id": "wxid_sender", "message_type": "text"},
                            "details": {"mentioned_self": False},
                        },
                        raw_event_id=122,
                        conversation_id=222,
                        message_id=322,
                    )

        execution_map = {item.candidate.plugin_code: item for item in report.executions}
        self.assertTrue(execution_map["weather"].ok)
        self.assertTrue(execution_map["weather"].debug["dispatch_stopped"])
        self.assertEqual(execution_map["weather"].debug["dispatch_stop_policy"], "consume")
        self.assertEqual(execution_map["message_summary"].status, "blocked")
        self.assertFalse(execution_map["message_summary"].attempted)
        self.assertEqual(execution_map["message_summary"].debug["blocked_by_plugin_code"], "weather")

    def test_ingest_inbound_event_persists_dispatch_summary(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            result = ingest_inbound_event(
                db,
                channel,
                {
                    "Event": "AddMsg",
                    "Wxid": "wxid_demo_account_001",
                    "Data": {
                        "FromUserName": {"string": "room-dispatch-save@chatroom"},
                        "ToUserName": {"string": "wxid_demo_account_001"},
                        "Content": {"string": "wxid_sender:\n/总结 保存调度摘要"},
                        "MsgType": 1,
                        "MsgId": "evt-dispatch-save-001",
                    },
                },
            )
            raw_event = db.get(RawWebhookEvent, result["raw_event_id"])
            self.assertIsNotNone(raw_event)
            stored_summary = json.loads(raw_event.dispatch_summary_json or "{}")

        self.assertTrue(result["mapped"])
        self.assertIn("executions", stored_summary)
        self.assertEqual(stored_summary["matched_plugin_codes"], ["message_summary"])
        execution_map = {item["plugin_code"]: item for item in stored_summary["executions"]}
        self.assertEqual(execution_map["message_summary"]["status"], "completed")
        self.assertEqual(execution_map["message_summary"]["debug"]["execution_source"], "directory_handler_executed")

    def test_ingest_inbound_event_auto_dispatches_plugin_reply(self):
        class FakeOutboundMessage:
            def __init__(self, message_id: int):
                self.id = message_id

        with patch(
            "app.services.channel_adapters.service.dispatch_outbound_message",
            return_value=(
                FakeOutboundMessage(9001),
                {
                    "ok": True,
                    "provider": "wechat_ipad",
                    "status": "sent",
                    "transport_status": "sent",
                    "external_status": "0",
                    "external_message_id": "auto-msg-1",
                    "endpoint": IPAD_874_CONTRACT.endpoints.message_send_text,
                },
            ),
        ) as outbound_mock, patch(
            "app.services.wechat_ipad.service.record_wechat_ipad_send_event",
            return_value={},
        ):
            with SessionLocal() as db:
                channel = db.get(Channel, 1)
                self.assertIsNotNone(channel)
                result = ingest_inbound_event(
                    db,
                    channel,
                    {
                        "Event": "AddMsg",
                        "Wxid": "wxid_demo_account_001",
                        "Data": {
                            "FromUserName": {"string": "room-dispatch-auto@chatroom"},
                            "ToUserName": {"string": "wxid_demo_account_001"},
                            "Content": {"string": "wxid_sender:\n/总结 自动出站"},
                            "MsgType": 1,
                            "MsgId": "evt-dispatch-auto-001",
                        },
                    },
                )
                raw_event = db.get(RawWebhookEvent, result["raw_event_id"])
                self.assertIsNotNone(raw_event)
                stored_summary = json.loads(raw_event.dispatch_summary_json or "{}")

        self.assertEqual(outbound_mock.call_count, 1)
        execution_map = {item["plugin_code"]: item for item in stored_summary["executions"]}
        auto_outbound = execution_map["message_summary"]["debug"]["auto_outbound"]
        self.assertEqual(len(auto_outbound), 1)
        self.assertTrue(auto_outbound[0]["ok"])
        self.assertEqual(auto_outbound[0]["payload"]["message_type"], "text")
        self.assertEqual(
            auto_outbound[0]["adapter_result"]["endpoint"],
            IPAD_874_CONTRACT.endpoints.message_send_text,
        )

    def test_auto_outbound_payloads_support_explicit_image_path_reply(self):
        image_file = Path(TEST_DIR) / "auto-reply-image.bin"
        image_file.write_bytes(b"fake-image-binary")
        payloads = _build_auto_outbound_payloads(
            # 回复归一化会按渠道类型决定媒体载荷结构，测试上下文必须提供真实协议边界。
            type(
                "FakeContext",
                (),
                {
                    "conversation_external_id": "room-image@chatroom",
                    "content": "test",
                    "channel_type": "wechat_ipad",
                },
            )(),
            {"replies": [{"type": "image", "image_path": str(image_file)}]},
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["message_type"], "image")
        self.assertTrue(payloads[0]["base64"])

    def test_ingest_inbound_event_persists_blocked_and_deferred_dispatch_statuses(self):
        original_discover_plugins = discover_plugins

        def fake_discover_plugins(*, force: bool = False):
            modified = []
            for item in original_discover_plugins(force=force):
                if item.manifest.code == "weather":
                    modified.append(
                        replace(
                            item,
                            capability=replace(item.capability, priority=130, consume_policy="stop"),
                            matcher=replace(item.matcher, commands=("总结",)),
                        )
                    )
                    continue
                modified.append(item)
            return tuple(modified)

        with patch("app.services.plugin_directory.discover_plugins", side_effect=fake_discover_plugins):
            with SessionLocal() as db:
                plugin = db.scalar(select(Plugin).where(Plugin.code == "weather"))
                self.assertIsNotNone(plugin)
                binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
                self.assertIsNotNone(binding)
                binding.status = "enabled"
                binding.config_json = json.dumps(
                    {
                        "commands": ["总结"],
                        "api_base": "https://weather.example.test",
                        "default_city": "香港",
                    }
                )
                db.commit()

                channel = db.get(Channel, 1)
                self.assertIsNotNone(channel)
                loaded_weather = load_directory_plugin("weather", force=True)
                self.assertIsNotNone(loaded_weather)
                weather_module = loaded_weather.instance._module
                with patch.object(weather_module, "_validate_runtime_config", return_value=None), patch.object(
                    weather_module,
                    "_fetch_weather_bundle",
                    return_value=self._fake_weather_snapshot(),
                ):
                    result = ingest_inbound_event(
                        db,
                        channel,
                        {
                            "Event": "AddMsg",
                            "Wxid": "wxid_demo_account_001",
                            "Data": {
                                "FromUserName": {"string": "room-dispatch-save-2@chatroom"},
                                "ToUserName": {"string": "wxid_demo_account_001"},
                                "Content": {"string": "wxid_sender:\n/总结 保存 blocked deferred"},
                                "MsgType": 1,
                                "MsgId": "evt-dispatch-save-002",
                            },
                        },
                    )
                raw_event = db.get(RawWebhookEvent, result["raw_event_id"])
                self.assertIsNotNone(raw_event)
                stored_summary = json.loads(raw_event.dispatch_summary_json or "{}")

        execution_map = {item["plugin_code"]: item for item in stored_summary["executions"]}
        self.assertEqual(execution_map["weather"]["status"], "completed")
        self.assertTrue(execution_map["weather"]["debug"]["dispatch_stopped"])
        self.assertEqual(execution_map["weather"]["debug"]["dispatch_stop_policy"], "stop")
        self.assertEqual(execution_map["message_summary"]["status"], "blocked")
        self.assertEqual(execution_map["message_summary"]["debug"]["blocked_by_plugin_code"], "weather")

    def test_ingest_inbound_event_persists_timeout_dispatch_status(self):
        original_loader = load_directory_plugin
        original_discover_plugins = discover_plugins

        class SlowPlugin:
            def handle_event(self, context):
                time.sleep(0.05)
                return {"ok": True, "status": "completed", "message": "不应该执行到这里"}

        def fake_loader(plugin_code: str | None, *, force: bool = False):
            if plugin_code == "message_summary":
                discovered = original_discover_plugins(force=force)
                entrypoint = next(item.entrypoint_path for item in discovered if item.manifest.code == "message_summary")
                return LoadedDirectoryPlugin(
                    plugin_code="message_summary",
                    module_name="test_message_summary_timeout_persist",
                    entrypoint_path=entrypoint,
                    instance=SlowPlugin(),
                    supports_matcher=False,
                    supports_handler=True,
                )
            return original_loader(plugin_code, force=force)

        def fake_discover_plugins(*, force: bool = False):
            modified = []
            for item in original_discover_plugins(force=force):
                if item.manifest.code == "message_summary":
                    modified.append(replace(item, capability=replace(item.capability, timeout_ms=10)))
                    continue
                modified.append(item)
            return tuple(modified)

        with patch("app.services.plugin_directory.load_directory_plugin", side_effect=fake_loader):
            with patch("app.services.plugin_directory.discover_plugins", side_effect=fake_discover_plugins):
                with SessionLocal() as db:
                    channel = db.get(Channel, 1)
                    self.assertIsNotNone(channel)
                    result = ingest_inbound_event(
                        db,
                        channel,
                        {
                            "Event": "AddMsg",
                            "Wxid": "wxid_demo_account_001",
                            "Data": {
                                "FromUserName": {"string": "room-dispatch-timeout@chatroom"},
                                "ToUserName": {"string": "wxid_demo_account_001"},
                                "Content": {"string": "wxid_sender:\n/总结 保存 timeout 摘要"},
                                "MsgType": 1,
                                "MsgId": "evt-dispatch-timeout-001",
                            },
                        },
                    )
                    raw_event = db.get(RawWebhookEvent, result["raw_event_id"])
                    self.assertIsNotNone(raw_event)
                    stored_summary = json.loads(raw_event.dispatch_summary_json or "{}")

        execution_map = {item["plugin_code"]: item for item in stored_summary["executions"]}
        self.assertEqual(execution_map["message_summary"]["status"], "timeout")
        self.assertTrue(execution_map["message_summary"]["debug"]["timeout_enforced"])
        self.assertEqual(execution_map["message_summary"]["debug"]["timeout_ms"], 10)

    def test_app_main_module_imports_cleanly(self):
        module = import_module("app.main")
        self.assertEqual(module.app.title, "Fulcrum")
        self.assertTrue(any(getattr(route, "path", None) == "/healthz" for route in module.app.routes))

    def test_plugin_runtime_package_imports_cleanly(self):
        module = import_module("app.services.plugin_runtime")
        self.assertTrue(hasattr(module, "execute_binding_action"))
        self.assertTrue(hasattr(module, "get_plugin_runtime_meta"))

    def test_plugin_config_view_separates_sensitive_fields_and_keeps_unknown_preview_fields(self):
        view = build_plugin_config_view(
            "dify",
            json.dumps(
                {
                    "api_base": "https://api.dify.example/v1",
                    "workflow_id": "workflow-001",
                    "commands": ["ai"],
                    "custom_timeout": 15,
                }
            ),
            secret_config={"api_key": "app-secret-001"},
        )

        self.assertEqual(view["mode"], "schema")
        self.assertIn('"custom_timeout": 15', view["preview_json"])
        self.assertNotIn("api_key", view["preview_json"])
        api_key_field = next(item for item in view["fields"] if item["name"] == "api_key")
        self.assertEqual(api_key_field["render_value"], "")
        self.assertTrue(api_key_field["secret_configured"])

    def test_plugin_detail_renders_schema_fields(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "dify"))
            self.assertIsNotNone(plugin)
            plugin_id = plugin.id

        response = self.client.get(f"/plugins/{plugin_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("API Base", response.text)
        self.assertIn("API Key", response.text)
        self.assertIn("Workflow ID", response.text)
        self.assertIn("触发命令", response.text)
        self.assertIn("结构化表单", response.text)
        self.assertIn("当前状态：未配置", response.text)
        self.assertNotIn("掩码 JSON 预览", response.text)
        self.assertIn("schema", response.text)

    def test_plugins_page_focuses_on_scan_results_and_runtime_navigation(self):
        self.login("ops", "ops123")
        response = self.client.get("/plugins")
        self.assertEqual(response.status_code, 200)
        self.assertIn("插件清单", response.text)
        self.assertIn("刷新插件目录", response.text)
        self.assertIn("运行状态页", response.text)
        self.assertIn("绑定插件到机器人", response.text)
        self.assertNotIn("当前页面只保留目录扫描结果、目录元信息和快速绑定入口。", response.text)
        self.assertIn("/plugins/runtime", response.text)
        self.assertNotIn("最近执行", response.text)
        self.assertNotIn("执行摘要</span>", response.text)
        self.assertNotIn("/plugins/1/edit", response.text)

    def test_plugin_pages_show_manifest_validation_state(self):
        self.login("ops", "ops123")
        with tempfile.TemporaryDirectory(prefix="fulcrum-plugins-", dir=TEMP_ROOT) as plugin_root:
            plugin_root_path = Path(plugin_root)
            broken_dir = plugin_root_path / "broken_plugin"
            broken_dir.mkdir(parents=True, exist_ok=True)
            broken_dir.joinpath("config.toml").write_text(
                """
[plugin]
code = "other_code"
display_name = "坏插件展示"
plugin_type = "message"

[capability]
consume_policy = "shared"
""".strip(),
                encoding="utf-8",
            )
            broken_dir.joinpath("plugin.py").write_text("plugin = object()\n", encoding="utf-8")

            with self.override_settings(plugins_dir=str(plugin_root_path)):
                list_response = self.client.get("/plugins")
                self.assertEqual(list_response.status_code, 200)
                self.assertIn("Manifest 异常", list_response.text)
                self.assertIn("Manifest 告警", list_response.text)
                self.assertIn(">1</strong>", list_response.text)
                self.assertIn(">0</strong>", list_response.text)
                self.assertIn("invalid", list_response.text)
                self.assertIn("manifest 错误", list_response.text)
                self.assertIn("plugin.code 与目录名不一致", list_response.text)

                with SessionLocal() as db:
                    plugin = db.scalar(select(Plugin).where(Plugin.code == "broken_plugin"))
                    self.assertIsNotNone(plugin)
                    plugin_id = plugin.id

                detail_response = self.client.get(f"/plugins/{plugin_id}")
                self.assertEqual(detail_response.status_code, 200)
                self.assertIn("Manifest 校验", detail_response.text)
                self.assertIn("Manifest 错误", detail_response.text)
                self.assertIn("暂不可进入正常加载与执行链", detail_response.text)
                self.assertIn("consume_policy 非法：shared。", detail_response.text)

                runtime_response = self.client.get("/plugins/runtime")
                self.assertEqual(runtime_response.status_code, 200)
                self.assertIn("Manifest", runtime_response.text)
                self.assertIn("详情页可查看错误", runtime_response.text)

    def test_plugin_runtime_pages_show_latest_dispatch_summary(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            result = ingest_inbound_event(
                db,
                channel,
                {
                    "Event": "AddMsg",
                    "Wxid": "wxid_demo_account_001",
                    "Data": {
                        "FromUserName": {"string": "room-plugin-runtime@chatroom"},
                        "ToUserName": {"string": "wxid_demo_account_001"},
                        "Content": {"string": "wxid_sender:\n/总结 展示最近调度"},
                        "MsgType": 1,
                        "MsgId": "evt-plugin-runtime-page-001",
                    },
                },
            )
            self.assertTrue(result["mapped"])
            plugin = db.scalar(select(Plugin).where(Plugin.code == "message_summary"))
            self.assertIsNotNone(plugin)
            plugin_id = plugin.id

        runtime_response = self.client.get("/plugins/runtime")
        self.assertEqual(runtime_response.status_code, 200)
        self.assertIn("最近调度", runtime_response.text)
        self.assertIn("completed", runtime_response.text)
        self.assertIn("directory_handler_executed", runtime_response.text)

        detail_response = self.client.get(f"/plugins/{plugin_id}")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn("最近调度摘要", detail_response.text)
        self.assertIn("directory_handler_executed", detail_response.text)
        self.assertIn("message_summary 目录插件已接管命令消息。", detail_response.text)

    def test_plugin_schema_form_save_still_persists_into_config_json(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "dify"))
            self.assertIsNotNone(plugin)
            plugin_id = plugin.id

        bind_response = self.client.post(
            "/plugins/bind",
            data={
                "bot_id": "1",
                "plugin_id": str(plugin_id),
                "status": "enabled",
                "schema__api_base": "https://api.dify.example/v1",
                "schema__api_key": "app-secret-001",
                "schema__workflow_id": "workflow-001",
                "schema__commands": "ai,chat",
            },
            follow_redirects=False,
        )
        self.assertEqual(bind_response.status_code, 303)

        with SessionLocal() as db:
            binding = db.scalar(select(BotPlugin).where(BotPlugin.bot_id == 1, BotPlugin.plugin_id == plugin_id))
            self.assertIsNotNone(binding)
            saved = json.loads(binding.config_json or "{}")
            self.assertEqual(saved["api_base"], "https://api.dify.example/v1")
            self.assertEqual(saved["api_key"], "app-secret-001")
            self.assertEqual(saved["workflow_id"], "workflow-001")
            self.assertEqual(saved["commands"], ["ai", "chat"])
            binding_id = binding.id

        update_response = self.client.post(
            f"/plugins/{binding_id}/edit",
            data={
                "status": "enabled",
                "schema__api_base": "https://api.dify.example/v2",
                "schema__api_key": "",
                "schema__workflow_id": "workflow-002",
                "schema__commands": "assistant",
            },
            follow_redirects=False,
        )
        self.assertEqual(update_response.status_code, 303)

        with SessionLocal() as db:
            binding = db.get(BotPlugin, binding_id)
            self.assertIsNotNone(binding)
            saved = json.loads(binding.config_json or "{}")
            self.assertEqual(saved["api_base"], "https://api.dify.example/v2")
            self.assertEqual(saved["api_key"], "app-secret-001")
            self.assertEqual(saved["workflow_id"], "workflow-002")
            self.assertEqual(saved["commands"], ["assistant"])

    def test_plugin_schema_edit_keeps_unknown_fields(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "dify"))
            self.assertIsNotNone(plugin)
            binding = BotPlugin(
                bot_id=1,
                plugin_id=plugin.id,
                status="enabled",
                config_json=json.dumps(
                    {
                        "api_base": "https://api.dify.example/v1",
                        "api_key": "app-secret-001",
                        "workflow_id": "workflow-001",
                        "commands": ["ai"],
                        "custom_timeout": 30,
                    }
                ),
            )
            db.add(binding)
            db.commit()
            db.refresh(binding)
            binding_id = binding.id

        response = self.client.post(
            f"/plugins/{binding_id}/edit",
            data={
                "status": "enabled",
                "schema__api_base": "https://api.dify.example/v2",
                "schema__api_key": "",
                "schema__workflow_id": "workflow-002",
                "schema__commands": "assistant",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        with SessionLocal() as db:
            binding = db.get(BotPlugin, binding_id)
            self.assertIsNotNone(binding)
            saved = json.loads(binding.config_json or "{}")
            self.assertEqual(saved["custom_timeout"], 30)
            self.assertEqual(saved["api_key"], "app-secret-001")

    def test_weather_plugin_execution_endpoint_returns_real_runtime_result(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "weather"))
            self.assertIsNotNone(plugin)
            binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
            self.assertIsNotNone(binding)
            binding.status = "enabled"
            binding.config_json = json.dumps(
                {
                    "api_base": "https://weather.example.test",
                    "default_city": "香港",
                }
            )
            db.commit()
            binding_id = binding.id

        loaded_weather = load_directory_plugin("weather", force=True)
        self.assertIsNotNone(loaded_weather)
        weather_module = loaded_weather.instance._module
        with patch.object(weather_module, "_validate_runtime_config", return_value=None), patch.object(
            weather_module,
            "_fetch_weather_bundle",
            return_value=self._fake_weather_snapshot(),
        ):
            response = self.client.post(
                f"/plugins/{binding_id}/execute",
                json={"city": "Shanghai"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["plugin_code"], "weather")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["output"]["city"], "上海")
        self.assertEqual(data["output"]["provider_status"], "completed")
        self.assertEqual(data["output"]["condition_day"], "多云")
        self.assertIn("上海天气", data["output"]["replies"][0]["content"])

        with SessionLocal() as db:
            binding = db.get(BotPlugin, binding_id)
            self.assertIsNotNone(binding)
            self.assertEqual(binding.last_execution_status, "completed")
            self.assertIn("已返回上海的天气", binding.last_execution_summary or "")
            self.assertIn("\"action\": \"execute\"", binding.runtime_details_json or "")

    def test_dify_plugin_execution_endpoint_uses_new_runtime_executor(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "dify"))
            self.assertIsNotNone(plugin)
            binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
            self.assertIsNotNone(binding)
            binding.status = "enabled"
            binding.config_json = json.dumps(
                {
                    "api_base": "https://api.dify.example/v1",
                    "api_key": "app-secret-001",
                    "commands": ["ai"],
                }
            )
            db.commit()
            binding_id = binding.id

        with patch(
            "app.services.plugin_runtime.executors.load_directory_plugin",
            return_value=type(
                "LoadedPlugin",
                (),
                {
                    "instance": type(
                        "PluginInstance",
                        (),
                        {
                            "execute_action": staticmethod(
                                lambda request: {
                                    "ok": True,
                                    "status": "completed",
                                    "message": "Dify 执行完成。",
                                    "output": {
                                        "provider_status": "completed",
                                        "reply_text": "来自 Dify 的回复",
                                        "replies": [{"type": "text", "content": "来自 Dify 的回复"}],
                                    },
                                }
                            )
                        },
                    )()
                },
            )(),
        ):
            response = self.client.post(
                f"/plugins/{binding_id}/execute",
                json={"query": "你好"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["plugin_code"], "dify")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["output"]["reply_text"], "来自 Dify 的回复")

        with SessionLocal() as db:
            binding = db.get(BotPlugin, binding_id)
            self.assertIsNotNone(binding)
            self.assertEqual(binding.last_execution_status, "completed")
            self.assertIn("来自 Dify 的回复", binding.runtime_details_json or "")

    def test_duanju_search_plugin_execution_endpoint_returns_provider_results(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "duanju_search"))
            self.assertIsNotNone(plugin)
            binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
            self.assertIsNotNone(binding)
            binding.status = "enabled"
            binding.config_json = json.dumps(
                {
                    "api_url": "https://search.example.test/v1/drama",
                    "result_limit": 3,
                    "timeout_seconds": 15,
                    "commands": ["短剧", "短剧搜索", "搜索短剧", "短剧查询"],
                }
            )
            db.commit()
            binding_id = binding.id

        with patch(
            "app.services.plugin_runtime.executors.load_directory_plugin",
            return_value=type(
                "LoadedPlugin",
                (),
                {
                    "instance": type(
                        "PluginInstance",
                        (),
                        {
                            "execute_action": staticmethod(
                                lambda request: {
                                    "ok": True,
                                    "status": "completed",
                                    "message": "短剧搜索完成，共返回 2 条结果。",
                                    "output": {
                                        "provider_status": "completed",
                                        "result_count": 2,
                                        "replies": [
                                            {
                                                "type": "text",
                                                "content": (
                                                    "短剧搜索：雪\n\n"
                                                    "1. 雪中来信\n链接：https://example.com/drama/1\n\n"
                                                    "2. 城市边缘\n链接：https://example.com/drama/2"
                                                ),
                                            }
                                        ],
                                    },
                                }
                            )
                        },
                    )()
                },
            )(),
        ):
            response = self.client.post(
                f"/plugins/{binding_id}/execute",
                json={"keyword": "雪"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["plugin_code"], "duanju_search")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["output"]["provider_status"], "completed")
        self.assertEqual(data["output"]["result_count"], 2)
        self.assertIn("雪中来信", data["output"]["replies"][0]["content"])

        with SessionLocal() as db:
            binding = db.get(BotPlugin, binding_id)
            self.assertIsNotNone(binding)
            self.assertEqual(binding.last_execution_status, "completed")
            self.assertIn("雪中来信", binding.runtime_details_json or "")
            self.assertIn("\"provider_status\": \"completed\"", binding.runtime_details_json or "")

    def test_music_plugin_execution_endpoint_uses_directory_runtime_logic(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "music"))
            self.assertIsNotNone(plugin)
            binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
            self.assertIsNotNone(binding)
            binding.status = "enabled"
            binding.config_json = json.dumps(
                {
                    "api_base": "https://music.example.test",
                    "quality": "exhigh",
                    "timeout_seconds": 15,
                }
            )
            db.commit()
            binding_id = binding.id

        with patch(
            "app.services.plugin_runtime.executors.load_directory_plugin",
            return_value=type(
                "LoadedPlugin",
                (),
                {
                    "instance": type(
                        "PluginInstance",
                        (),
                        {
                            "execute_action": staticmethod(
                                lambda request: {
                                    "ok": True,
                                    "status": "completed",
                                    "message": "音乐点播完成。",
                                    "output": {
                                        "provider_status": "completed",
                                        "song_name": "夜空中最亮的星",
                                        "artist_name": "逃跑计划",
                                        "replies": [
                                            {
                                                "type": "file",
                                                "file_name": "逃跑计划 - 夜空中最亮的星.mp3",
                                                "file_url": "https://media.example.test/song-001",
                                            }
                                        ],
                                    },
                                }
                            )
                        },
                    )()
                },
            )(),
        ):
            response = self.client.post(f"/plugins/{binding_id}/execute", json={"keyword": "夜空中最亮的星"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["plugin_code"], "music")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["output"]["provider_status"], "completed")
        self.assertEqual(data["output"]["replies"][0]["type"], "file")

    def test_ingest_inbound_event_updates_chatroom_member_last_active_at(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=1,
                channel_id=channel.id,
                external_id="room-active-test@chatroom",
                name="活跃测试群",
                status="active",
                member_count=1,
            )
            db.add(chatroom)
            db.flush()
            member = ChatroomMember(
                chatroom_id=chatroom.id,
                bot_id=1,
                channel_id=channel.id,
                member_external_id="wxid_active_user",
                nickname="活跃用户",
                display_name="活跃用户",
            )
            db.add(member)
            db.commit()

            event_ts = int(datetime(2026, 3, 25, 10, 0, 0, tzinfo=timezone.utc).timestamp())
            normalized = {
                "event_key": "active-001",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "room-active-test@chatroom",
                    "title": "活跃测试群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "今天我发言了",
                    "sender_id": "wxid_active_user",
                    "sender_name": "活跃用户",
                    "message_type": "text",
                    "external_message_id": "msg-active-001",
                },
                "details": {
                    "timestamp": event_ts,
                },
            }

            from app.services.channel_events import ingest_normalized_event

            result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)
            self.assertTrue(result["ok"])

        with SessionLocal() as db:
            member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.channel_id == 1,
                    ChatroomMember.member_external_id == "wxid_active_user",
                )
            )
            self.assertIsNotNone(member)
            self.assertIsNotNone(member.last_active_at)
            self.assertEqual(member.last_active_at.replace(tzinfo=timezone.utc), datetime(2026, 3, 25, 10, 0, 0, tzinfo=timezone.utc))

    def test_workwechat_file_ingest_exposes_attachment_to_plugin_payload(self):
        from app.models.entities import MessageAttachment
        from app.services.channel_events import ingest_normalized_event
        from app.services.task_queue import PLUGIN_DISPATCH_JOB, parse_job_payload

        with SessionLocal() as db:
            bot = db.scalar(select(Bot).order_by(Bot.id.asc()))
            if bot is None:
                bot = Bot(name="企微测试机器人")
                db.add(bot)
                db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微文件通道",
                channel_type="workwechat",
                api_base="http://127.0.0.1:8181",
                webhook_path="/webhooks/workwechat/inbound",
            )
            db.add(channel)
            db.flush()
            normalized = {
                "provider": "workwechat",
                "event_key": "ww-file-attachment-001",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:order-room",
                    "title": "订单群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[file] 订单.xlsx",
                    "sender_id": "staff-001",
                    "sender_name": "采购",
                    "message_type": "file",
                    "external_message_id": "srv-file-001",
                },
                "details": {
                    "timestamp": int(datetime(2026, 3, 25, 10, 0, 0, tzinfo=timezone.utc).timestamp()),
                    "file_name": "订单.xlsx",
                    "file_size": 2048,
                    "download_path": "D:\\workwechat\\downloads\\订单.xlsx",
                },
            }

            result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)
            self.assertTrue(result["ok"])
            self.assertTrue(result["mapped"])
            attachments = result["normalized"].get("attachments")
            self.assertIsInstance(attachments, list)
            self.assertEqual(attachments[0]["file_name"], "订单.xlsx")
            self.assertEqual(attachments[0]["storage_path"], "D:\\workwechat\\downloads\\订单.xlsx")

            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.channel_id == channel.id))
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment.status, "downloaded")
            self.assertEqual(attachment.file_size, 2048)

            job = db.scalar(
                select(TaskJob)
                .where(TaskJob.job_type == PLUGIN_DISPATCH_JOB, TaskJob.related_id == result["raw_event_id"])
                .order_by(TaskJob.id.desc())
            )
            self.assertIsNotNone(job)
            payload = parse_job_payload(job)
            job_attachments = payload["normalized_event"].get("attachments")
            self.assertEqual(job_attachments[0]["file_name"], "订单.xlsx")
            self.assertEqual(job_attachments[0]["status"], "downloaded")

    def test_workwechat_outbound_file_echo_keeps_attachment_direction_out(self):
        from app.models.entities import MessageAttachment
        from app.services.channel_events import ingest_normalized_event

        with SessionLocal() as db:
            bot = db.scalar(select(Bot).order_by(Bot.id.asc()))
            if bot is None:
                bot = Bot(name="企微出站回显机器人")
                db.add(bot)
                db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微出站回显通道",
                channel_type="workwechat",
                api_base="http://127.0.0.1:8181",
                webhook_path="/webhooks/workwechat/inbound",
            )
            db.add(channel)
            db.flush()
            normalized = {
                "provider": "workwechat",
                "event_key": "ww-file-outbound-echo-001",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:order-room",
                    "title": "订单群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[file] 发单.xls",
                    "sender_id": "self-user",
                    "sender_name": "小明",
                    "message_type": "file",
                    "external_message_id": "srv-file-out-001",
                    "direction": "out",
                },
                "details": {
                    "timestamp": int(datetime(2026, 6, 11, 2, 0, 0, tzinfo=timezone.utc).timestamp()),
                    "direction": "out",
                    "file_name": "发单.xls",
                    "file_size": 2048,
                    "download_error": "download_inbound_file_not_found: C:\\cache\\发单.xls",
                },
            }

            result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)
            self.assertTrue(result["ok"])
            attachments = result["normalized"].get("attachments")
            self.assertIsInstance(attachments, list)
            self.assertEqual(attachments[0]["direction"], "out")
            self.assertEqual(attachments[0]["status"], "failed")

            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.channel_id == channel.id))
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment.direction, "out")
            self.assertEqual(attachment.last_error, "download_inbound_file_not_found: C:\\cache\\发单.xls")

    def test_workwechat_failed_inbound_file_enqueues_cdn_compensation(self):
        from app.models.entities import MessageAttachment
        from app.services.channel_events import ingest_normalized_event
        from app.services.task_queue import INBOUND_MEDIA_FETCH_JOB, parse_job_payload

        with SessionLocal() as db:
            bot = db.scalar(select(Bot).order_by(Bot.id.asc()))
            if bot is None:
                bot = Bot(name="企微补偿下载机器人")
                db.add(bot)
                db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微补偿下载通道",
                channel_type="workwechat",
                api_base="http://127.0.0.1:8181",
                webhook_path="/webhooks/workwechat/inbound",
            )
            db.add(channel)
            db.flush()
            normalized = {
                "provider": "workwechat",
                "event_key": "ww-file-compensation-001",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:order-room",
                    "title": "订单群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[file] 回单.xlsx",
                    "sender_id": "staff-001",
                    "sender_name": "采购",
                    "message_type": "file",
                    "external_message_id": "srv-file-compensation-001",
                },
                "details": {
                    "timestamp": int(datetime(2026, 6, 14, 16, 0, 45, tzinfo=timezone.utc).timestamp()),
                    "direction": "in",
                    "file_name": "回单.xlsx",
                    "file_size": 4926,
                    "download_error": "download_inbound_file_not_found: C:\\cache\\回单.xlsx",
                    "cdn": {
                        "file_id": "file-id",
                        "aes_key": "aes-key",
                        "md5": "md5-value",
                        "size": 4926,
                    },
                    "raw_data": {
                        "server_id": "srv-file-compensation-001",
                        "cdn_type": 2,
                        "cdn": {
                            "file_id": "file-id",
                            "aes_key": "aes-key",
                            "md5": "md5-value",
                            "size": 4926,
                        },
                    },
                },
            }

            result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)
            self.assertTrue(result["ok"])
            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.channel_id == channel.id))
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment.status, "pending")
            self.assertEqual(attachment.last_error, "download_inbound_file_not_found: C:\\cache\\回单.xlsx")
            job = db.scalar(
                select(TaskJob)
                .where(TaskJob.job_type == INBOUND_MEDIA_FETCH_JOB, TaskJob.related_id == attachment.id)
                .order_by(TaskJob.id.desc())
            )
            self.assertIsNotNone(job)
            payload = parse_job_payload(job)
            self.assertEqual(payload["attachment_id"], attachment.id)
            self.assertEqual(payload["reason"], "workwechat_cdn_compensation")

            attachments = result["normalized"].get("attachments")
            self.assertIsInstance(attachments, list)
            self.assertEqual(attachments[0]["status"], "pending")

    def test_workwechat_cloud_failed_inbound_file_enqueues_cloud_compensation(self):
        from app.models.entities import MessageAttachment
        from app.services.channel_events import ingest_normalized_event
        from app.services.task_queue import INBOUND_MEDIA_FETCH_JOB, WORKWECHAT_CLOUD_CONTROL_JOB, parse_job_payload

        with SessionLocal() as db:
            bot = db.scalar(select(Bot).order_by(Bot.id.asc()))
            if bot is None:
                bot = Bot(name="企微云端补偿下载机器人")
                db.add(bot)
                db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微云端补偿下载通道",
                channel_type="workwechat",
                adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll", "client_token_hash": "hash"}, ensure_ascii=False),
            )
            db.add(channel)
            db.flush()
            normalized = {
                "provider": "workwechat",
                "event_key": "ww-cloud-file-compensation-001",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:order-room",
                    "title": "订单群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[file] 回单.xlsx",
                    "sender_id": "staff-001",
                    "sender_name": "采购",
                    "message_type": "file",
                    "external_message_id": "srv-cloud-file-compensation-001",
                },
                "details": {
                    "direction": "in",
                    "file_name": "回单.xlsx",
                    "file_size": 4926,
                    "download_error": "download_inbound_file_not_found: C:\\cache\\回单.xlsx",
                    "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926},
                    "raw_data": {
                        "server_id": "srv-cloud-file-compensation-001",
                        "cdn_type": 2,
                        "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926},
                    },
                },
            }

            result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)
            self.assertTrue(result["ok"])
            attachment = db.scalar(select(MessageAttachment).where(MessageAttachment.channel_id == channel.id))
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment.status, "pending")
            local_jobs = db.scalars(select(TaskJob).where(TaskJob.job_type == INBOUND_MEDIA_FETCH_JOB)).all()
            self.assertEqual(local_jobs, [])
            job = db.scalar(select(TaskJob).where(TaskJob.job_type == WORKWECHAT_CLOUD_CONTROL_JOB, TaskJob.related_id == attachment.id))
            self.assertIsNotNone(job)
            payload = parse_job_payload(job)
            self.assertEqual(payload["action"], "download_inbound_media")
            self.assertEqual(payload["attachment_id"], attachment.id)
            self.assertEqual(payload["payload"]["data"]["cdn_type"], 2)

    def test_workwechat_cdn_compensation_worker_updates_attachment(self):
        from app.models.entities import MessageAttachment
        from app.services.inbound_media import download_wechat_media_attachment

        stored_path = str(Path(TEST_DIR) / "inbound_files" / "2" / "share" / "回单.xlsx")
        captured: dict[str, object] = {}

        def fake_post_json(self, url, body, *, headers, timeout):
            captured["url"] = url
            captured["body"] = body
            captured["headers"] = headers
            captured["timeout"] = timeout
            return {
                "ok": True,
                "download_path": stored_path,
                "file_size": 4926,
                "mime_type": "application/octet-stream",
                "download_url": "http://main.test/inbound-files/2/share/return.xlsx",
                "source_path": "C:\\cache\\回单.xlsx",
            }

        with SessionLocal() as db:
            bot = db.scalar(select(Bot).order_by(Bot.id.asc()))
            if bot is None:
                bot = Bot(name="企微补偿下载机器人")
                db.add(bot)
                db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微补偿下载通道",
                channel_type="workwechat",
                api_base="http://127.0.0.1:8181",
                adapter_config_json=json.dumps({"headers": {"X-Provider-Token": "provider-token"}}, ensure_ascii=False),
            )
            db.add(channel)
            db.flush()
            attachment = MessageAttachment(
                message_id=123,
                channel_id=channel.id,
                conversation_id=456,
                direction="in",
                attachment_type="file",
                provider="workwechat",
                status="pending",
                file_size=4926,
                last_error="download_inbound_file_not_found: C:\\cache\\回单.xlsx",
                metadata_json=json.dumps(
                    {
                        "source_event_key": "srv-file-compensation-001",
                        "file_name": "回单.xlsx",
                        "details": {
                            "file_name": "回单.xlsx",
                            "file_size": 4926,
                            "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926},
                            "raw_data": {
                                "server_id": "srv-file-compensation-001",
                                "cdn_type": 2,
                                "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926},
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            db.add(attachment)
            db.flush()
            with patch("app.services.inbound_media.WorkWechatAdapter._post_json", new=fake_post_json):
                result = download_wechat_media_attachment(db, attachment=attachment, channel=channel)

            self.assertEqual(result["storage_path"], stored_path)
            self.assertTrue(result["compensation"])
            self.assertEqual(attachment.status, "downloaded")
            self.assertEqual(attachment.storage_path, stored_path)
            self.assertIsNone(attachment.last_error)

        self.assertEqual(captured["url"], "http://127.0.0.1:8181/inbound-media/download")
        self.assertEqual(captured["headers"]["X-Provider-Token"], "provider-token")
        body = captured["body"]
        self.assertEqual(body["message_type"], "file")
        self.assertEqual(body["data"]["cdn_type"], 2)
        self.assertEqual(body["data"]["cdn"]["file_id"], "file-id")

    def test_workwechat_cloud_cdn_compensation_worker_queues_control_job_without_api_base(self):
        from app.models.entities import MessageAttachment
        from app.services.inbound_media import download_wechat_media_attachment
        from app.services.task_queue import WORKWECHAT_CLOUD_CONTROL_JOB, parse_job_payload

        with SessionLocal() as db:
            bot = db.scalar(select(Bot).order_by(Bot.id.asc()))
            if bot is None:
                bot = Bot(name="企微云端历史补偿机器人")
                db.add(bot)
                db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微云端历史补偿通道",
                channel_type="workwechat",
                adapter_config_json=json.dumps({"workwechat_delivery_mode": "cloud_poll", "client_token_hash": "hash"}, ensure_ascii=False),
            )
            db.add(channel)
            db.flush()
            attachment = MessageAttachment(
                message_id=123,
                channel_id=channel.id,
                conversation_id=456,
                direction="in",
                attachment_type="file",
                provider="workwechat",
                status="failed",
                file_size=4926,
                last_error="download_inbound_file_not_found: C:\\cache\\回单.xlsx",
                metadata_json=json.dumps(
                    {
                        "source_event_key": "srv-cloud-worker-001",
                        "file_name": "回单.xlsx",
                        "details": {
                            "file_name": "回单.xlsx",
                            "file_size": 4926,
                            "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926},
                            "raw_data": {"cdn_type": 2, "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926}},
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            db.add(attachment)
            db.flush()

            result = download_wechat_media_attachment(db, attachment=attachment, channel=channel)

            self.assertTrue(result["queued_cloud_compensation"])
            self.assertEqual(attachment.status, "pending")
            job = db.scalar(select(TaskJob).where(TaskJob.job_type == WORKWECHAT_CLOUD_CONTROL_JOB, TaskJob.related_id == attachment.id))
            self.assertIsNotNone(job)
            payload = parse_job_payload(job)
            self.assertEqual(payload["action"], "download_inbound_media")
            self.assertEqual(payload["payload"]["data"]["cdn"]["file_id"], "file-id")

    def test_workwechat_provider_uploads_inbound_file_to_main_service(self):
        from app.services.channel_events import ingest_normalized_event
        from app.services.inbound_files import INBOUND_FILE_ROOT

        with SessionLocal() as db:
            bot = db.scalar(select(Bot).order_by(Bot.id.asc()))
            if bot is None:
                bot = Bot(name="企微上传测试机器人")
                db.add(bot)
                db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微上传通道",
                channel_type="workwechat",
                api_base="http://127.0.0.1:8181",
                webhook_path="/webhooks/workwechat/inbound",
                adapter_config_json=json.dumps(
                    {"headers": {"X-Provider-Token": "replace-with-provider-token"}},
                    ensure_ascii=False,
                ),
            )
            db.add(channel)
            db.flush()
            channel_id = channel.id
            db.commit()

        content = b"order-file-bytes"
        response = self.client.post(
            f"/webhooks/{channel_id}/inbound-upload",
            headers={"X-Provider-Token": "replace-with-provider-token"},
            json={
                "message_type": "file",
                "file_name": "导入赔付单20260604-1707.xls",
                "content_type": "application/octet-stream",
                "source_event_key": "1154891",
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["storage_path"])
        stored_path = Path(data["storage_path"])
        self.assertTrue(stored_path.exists())
        self.assertEqual(stored_path.read_bytes(), content)
        self.assertTrue(str(stored_path).startswith(str(INBOUND_FILE_ROOT)))

        with SessionLocal() as db:
            channel = db.get(Channel, channel_id)
            self.assertIsNotNone(channel)
            normalized = {
                "provider": "workwechat",
                "event_key": "1154891",
                "event_type": "message",
                "should_persist_message": True,
                "conversation": {
                    "external_id": "R:10699268081551545",
                    "title": "订单群",
                    "chat_type": "group",
                },
                "message": {
                    "content": "[file] 导入赔付单20260604-1707.xls",
                    "sender_id": "7881301246927444",
                    "sender_name": "邦釘",
                    "message_type": "file",
                    "external_message_id": "1154891",
                },
                "details": {
                    "timestamp": int(datetime(2026, 6, 5, 6, 45, 57, tzinfo=timezone.utc).timestamp()),
                    "file_name": "导入赔付单20260604-1707.xls",
                    "file_size": len(content),
                    "download_path": data["storage_path"],
                    "download_url": data["download_url"],
                },
            }
            result = ingest_normalized_event(db, channel, normalized, source="unit-test", raw_payload=normalized)
            self.assertTrue(result["ok"])
            attachments = result["normalized"].get("attachments")
            self.assertIsInstance(attachments, list)
            self.assertEqual(attachments[0]["file_name"], "导入赔付单20260604-1707.xls")
            self.assertEqual(attachments[0]["storage_path"], data["storage_path"])
            self.assertEqual(attachments[0]["status"], "downloaded")

    def test_wechat_ipad_adapter_parse_mod_contacts_chatroom_ids(self):
        from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter

        adapter = WechatIpadAdapter()
        group_ids = adapter.parse_mod_contacts_msg(
            {
                "ModContacts": [
                    {"UserName": {"string": "room-a@chatroom"}},
                    {"UserName": "room-b@chatroom"},
                    {"UserName": {"string": "wxid_user_001"}},
                    {"UserName": {"string": "room-a@chatroom"}},
                ]
            }
        )
        self.assertEqual(group_ids, ["room-a@chatroom", "room-b@chatroom"])

    def test_douyin_parser_plugin_execution_endpoint_uses_directory_runtime_logic(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "douyin_parser"))
            self.assertIsNotNone(plugin)
            binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
            self.assertIsNotNone(binding)
            binding.status = "enabled"
            binding.config_json = json.dumps(
                {
                    "dtk_api_url": "https://parser.example.test",
                    "timeout_seconds": 20,
                }
            )
            db.commit()
            binding_id = binding.id

        with patch(
            "app.services.plugin_runtime.executors.load_directory_plugin",
            return_value=type(
                "LoadedPlugin",
                (),
                {
                    "instance": type(
                        "PluginInstance",
                        (),
                        {
                            "execute_action": staticmethod(
                                lambda request: {
                                    "ok": True,
                                    "status": "completed",
                                    "message": "已解析抖音视频。",
                                    "output": {
                                        "provider_status": "completed",
                                        "media_type": "video",
                                        "replies": [
                                            {
                                                "type": "file",
                                                "file_name": "测试作者 - 测试视频.mp4",
                                                "file_url": "https://media.example.test/douyin-001.mp4",
                                            }
                                        ],
                                    },
                                }
                            )
                        },
                    )()
                },
            )(),
        ):
            response = self.client.post(
                f"/plugins/{binding_id}/execute",
                json={"url": "https://v.douyin.com/abcdefg/"},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["plugin_code"], "douyin_parser")
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["output"]["provider_status"], "completed")
        self.assertEqual(data["output"]["replies"][0]["type"], "file")

    def test_douyu_directory_plugin_executes_room_status_and_plan_flow(self):
        loaded = load_directory_plugin("douyu", force=True)
        self.assertIsNotNone(loaded)
        self.assertIsNotNone(loaded.instance)
        module = loaded.instance._module

        class FakeHttpClient:
            def request(self, method, url, **kwargs):
                self.assert_request(method, url, kwargs)
                return PluginHttpResponse(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    body=json.dumps(
                        {
                            "room": {
                                "show_status": 1,
                                "videoLoop": 0,
                                "nickname": "Streamer A",
                                "room_name": "Test Live",
                            }
                        }
                    ).encode("utf-8"),
                    url=url,
                )

            @staticmethod
            def assert_request(method, url, kwargs):
                if method != "GET" or "betard" not in url:
                    raise AssertionError(f"unexpected request: {method} {url}")
                if kwargs["max_response_bytes"] != 2 * 1024 * 1024:
                    raise AssertionError("missing response size limit")

        with patch.object(module, "PluginHttpClient", return_value=FakeHttpClient()):
            status_result = loaded.instance.execute_action(
                PluginExecutionRequest(
                    action="execute",
                    config={"api_url_template": "https://www.douyu.com/betard/{room_id}"},
                    payload={"operation": "room_status", "room_id": "12345"},
                )
            )
        status_output = status_result["output"] if isinstance(status_result, dict) else status_result.output
        self.assertEqual(status_output["provider_status"], "live")
        self.assertEqual(status_output["room_id"], "12345")
        self.assertEqual(status_output["replies"][0]["type"], "text")
        self.assertIn("https://www.douyu.com/12345", status_output["replies"][0]["content"])
        self.assertNotIn("douyu_xml", status_output)

        plan_result = loaded.instance.execute_action(
            PluginExecutionRequest(
                action="execute",
                config={},
                payload={"operation": "subscribe_room", "room_id": "12345", "conversation_id": 999},
            )
        )
        plan_output = plan_result["output"] if isinstance(plan_result, dict) else plan_result.output
        self.assertEqual(plan_output["provider_status"], "trusted_context_required")

    def test_plugin_reload_uses_new_runtime_for_migrated_plugins(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            plugin = db.scalar(select(Plugin).where(Plugin.code == "message_summary"))
            self.assertIsNotNone(plugin)
            binding = db.scalar(select(BotPlugin).where(BotPlugin.plugin_id == plugin.id, BotPlugin.bot_id == 1))
            self.assertIsNotNone(binding)
            binding.status = "enabled"
            binding.config_json = json.dumps({"max_messages": 25, "include_mentions": True})
            db.commit()
            binding_id = binding.id

        response = self.client.post(f"/plugins/{binding_id}/reload", follow_redirects=False)
        self.assertEqual(response.status_code, 303)

        with SessionLocal() as db:
            binding = db.get(BotPlugin, binding_id)
            self.assertIsNotNone(binding)
            self.assertEqual(binding.runtime_status, "running")
            self.assertIsNotNone(binding.last_reload_at)

    def test_plugin_runtime_page_shows_operational_capabilities_without_migration_copy(self):
        self.login("ops", "ops123")
        response = self.client.get("/plugins/runtime")
        self.assertEqual(response.status_code, 200)
        self.assertIn("运行时注册表", response.text)
        self.assertIn("functional", response.text)
        self.assertIn("reload, execute", response.text)
        self.assertNotIn("迁移状态", response.text)

    def test_wechat_ipad_overview_builds_operator_friendly_sync_diagnostics(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.last_message_sync_at = datetime.fromisoformat("2026-03-23T11:20:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "sync_key": "sync-key-ops-001",
                    "sync_continue_flag": 0,
                    "last_message_sync_count": 5,
                    "last_message_sync_ingested": 3,
                    "last_message_sync_deduplicated": 2,
                    "receive_total": 12,
                    "dedupe_total": 4,
                    "last_error": "sync timeout once",
                    "last_pull_protocol_summary": {
                        "message_count": 5,
                        "continue_flag": 0,
                        "sync_key": "sync-key-ops-001",
                        "first_event": "AddMsg",
                    },
                    "last_inbound_protocol_summary": {
                        "source": "wechat_ipad_sync",
                        "event_key": "evt-ops-001",
                        "mapped": True,
                        "deduplicated": False,
                    },
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            overview = build_wechat_ipad_overview(channel)

        diagnostics = overview["sync_diagnostics"]
        self.assertEqual(diagnostics["sync_key"], "sync-key-ops-001")
        self.assertEqual(diagnostics["continue_flag"], "已到末尾 (0)")
        self.assertEqual(diagnostics["last_sync_count"], 5)
        self.assertEqual(diagnostics["last_sync_ingested"], 3)
        self.assertEqual(diagnostics["last_sync_deduplicated"], 2)
        self.assertEqual(diagnostics["last_error"], "sync timeout once")
        self.assertEqual(diagnostics["state"], "warning")
        self.assertEqual(diagnostics["state_label"], "最近同步异常")
        self.assertIn("sync timeout once", diagnostics["operator_hint"])
        self.assertEqual(diagnostics["operator_snapshot"]["status_key"], "recent_error")
        self.assertEqual(diagnostics["operator_snapshot"]["headline"], "当前同步档位：最近同步异常")
        self.assertIn("最近一次同步链路出现异常", diagnostics["operator_snapshot"]["summary"])
        self.assertIn("sync_key=sync-key-ops-001", diagnostics["operator_snapshot"]["detail"])
        self.assertIn("分类=最近同步异常", diagnostics["operator_summary"])
        self.assertIn("sync_key=sync-key-ops-001", diagnostics["operator_summary"])
        self.assertIn("continue_flag=已到末尾 (0)", diagnostics["operator_summary"])
        self.assertIn("拉取/新增/去重=5/3/2", diagnostics["operator_summary"])
        self.assertIn("异常=sync timeout once", diagnostics["operator_summary"])
        self.assertIn("消息数=5", diagnostics["last_pull_protocol_summary"])
        self.assertIn("来源=wechat_ipad_sync", diagnostics["last_inbound_protocol_summary"])

    def test_wechat_ipad_pull_messages_auto_syncs_new_chatroom_members(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            adapter_config = json.loads(channel.adapter_config_json or "{}")
            adapter_config["wxid"] = "wxid_demo_account_001"
            channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False)
            channel.runtime_info_json = json.dumps({"active_wxid": "wxid_demo_account_001"}, ensure_ascii=False)
            db.commit()
            db.refresh(channel)

            with patch(
                "app.services.wechat_ipad.client.WechatIpadApiClient.request_json",
                side_effect=[
                    {
                        "Ret": 0,
                        "Data": {
                            "AddMsgs": [
                                {
                                    "Event": "AddMsg",
                                    "Wxid": "wxid_demo_account_001",
                                    "Data": {
                                        "FromUserName": {"string": "room-auto-sync@chatroom"},
                                        "ToUserName": {"string": "wxid_demo_account_001"},
                                        "Content": {"string": "你邀请张三加入了群聊"},
                                        "MsgType": 10000,
                                        "MsgId": "sync-chatroom-evt-001",
                                    },
                                }
                            ],
                            "Synckey": "sync-key-chatroom-001",
                            "ContinueFlag": 0,
                        },
                        "http_status": 200,
                    },
                    {
                        "Ret": 0,
                        "Data": {
                            "ContactList": [
                                {
                                    "UserName": "room-auto-sync@chatroom",
                                    "NickName": "自动同步群",
                                    "MemberCount": 2,
                                    "ChatRoomOwner": "wxid_owner_auto_sync",
                                }
                            ]
                        },
                        "http_status": 200,
                    },
                    {
                        "Ret": 0,
                        "Data": {
                            "NewChatroomData": {
                                "ChatRoomMember": [
                                    {
                                        "UserName": "wxid_owner_auto_sync",
                                        "NickName": "群主",
                                        "DisplayName": "群主名片",
                                        "MemberFlag": "1",
                                    },
                                    {
                                        "UserName": "wxid_member_auto_sync",
                                        "NickName": "张三",
                                        "DisplayName": "张三名片",
                                        "MemberFlag": "0",
                                    },
                                ]
                            }
                        },
                        "http_status": 200,
                    },
                ],
            ):
                wechat_ipad_service.pull_wechat_ipad_messages(db, channel)

        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            runtime_info = json.loads(channel.runtime_info_json or "{}")
            self.assertEqual(runtime_info.get("last_message_sync_chatroom_candidate_count"), 1)
            self.assertTrue(runtime_info.get("last_message_sync_chatroom_sync_triggered"))
            self.assertIn("成功补齐群资料 1 个", runtime_info.get("last_chatroom_sync_summary") or "")

            candidate_signals = runtime_info.get("last_message_sync_chatroom_candidates") or []
            self.assertEqual(len(candidate_signals), 1)
            self.assertEqual(candidate_signals[0]["reason"], "member_change_event")
            self.assertEqual(candidate_signals[0]["chatroom_id"], "room-auto-sync@chatroom")

            chatroom = db.scalar(select(Chatroom).where(Chatroom.external_id == "room-auto-sync@chatroom"))
            self.assertIsNotNone(chatroom)
            self.assertEqual(chatroom.name, "自动同步群")
            self.assertEqual(chatroom.status, "members_synced")
            self.assertEqual(chatroom.member_count, 2)

            members = db.scalars(
                select(ChatroomMember)
                .where(ChatroomMember.chatroom_id == chatroom.id)
                .order_by(ChatroomMember.member_external_id.asc())
            ).all()
            self.assertEqual(len(members), 2)
            self.assertEqual(members[0].member_external_id, "wxid_member_auto_sync")
            self.assertEqual(members[1].member_external_id, "wxid_owner_auto_sync")

    def test_wechat_ipad_action_api_runs_operator_action_and_persists_summary(self):
        self.login("ops", "ops123")
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            adapter_config = json.loads(channel.adapter_config_json or "{}")
            adapter_config["wxid"] = "wxid_demo_account_001"
            channel.adapter_config_json = json.dumps(adapter_config, ensure_ascii=False)
            channel.runtime_info_json = json.dumps({"active_wxid": "wxid_demo_account_001"}, ensure_ascii=False)
            db.commit()

        with patch(
            "app.services.wechat_ipad.client.WechatIpadApiClient.request_json",
            return_value={
                "Success": True,
                "Data": {
                    "ContactUsernameList": ["wxid_a", "wxid_b"],
                    "CurrentWxcontactSeq": 12,
                },
                "http_status": 200,
            },
        ):
            response = self.client.post(
                "/channels/1/wechat-ipad/actions/get_contact_list",
                json={"current_wxcontact_seq": 10, "current_chatroom_contact_seq": 3},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["action"], "get_contact_list")
        self.assertIn("get_contact_list", data["summary"])

        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            runtime_info = json.loads(channel.runtime_info_json or "{}")
            self.assertEqual(runtime_info.get("last_action_name"), "get_contact_list")
            self.assertTrue(runtime_info.get("last_action_ok"))
            self.assertIn("调用成功", runtime_info.get("last_action_summary") or "")
            self.assertEqual(runtime_info.get("last_action_params", {}).get("current_wxcontact_seq"), 10)

    def test_wechat_ipad_overview_exposes_action_console(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_action_name": "get_contact_list",
                    "last_action_at": "2026-03-24T03:30:00+00:00",
                    "last_action_ok": True,
                    "last_action_summary": "wechat_ipad 动作 get_contact_list 调用成功。",
                    "last_action_params": {"current_wxcontact_seq": 10},
                    "last_action_result_preview": {"Success": True, "data_keys": ["ContactUsernameList"]},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel)

        action_console = overview["action_console"]
        self.assertEqual(action_console["last_action_name"], "get_contact_list")
        self.assertTrue(action_console["last_action_ok"])
        self.assertEqual(action_console["endpoint_template"], "/channels/1/wechat-ipad/actions/__ACTION__")
        supported_keys = {item["key"] for item in action_console["supported_actions"]}
        self.assertIn("get_contact_list", supported_keys)
        self.assertIn("get_chatroom_announce", supported_keys)
        self.assertIn("accept_friend", supported_keys)
        self.assertEqual(action_console["last_action_params"]["current_wxcontact_seq"], 10)

    def test_wechat_ipad_overview_marks_sync_diagnostics_empty_state(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps({}, ensure_ascii=False)
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel)

        diagnostics = overview["sync_diagnostics"]
        self.assertEqual(diagnostics["state"], "empty")
        self.assertEqual(diagnostics["state_label"], "尚未形成同步样本")
        self.assertIn("还没有最近同步记录", diagnostics["operator_hint"])
        self.assertEqual(diagnostics["operator_snapshot"]["status_key"], "no_sample")
        self.assertEqual(diagnostics["operator_snapshot"]["status_tone"], "muted")
        self.assertIn("当前更像是尚未完成首次状态同步或消息拉取", diagnostics["operator_snapshot"]["summary"])

    def test_wechat_ipad_overview_marks_sync_diagnostics_healthy_without_new_messages(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.last_message_sync_at = datetime.fromisoformat("2026-03-23T11:45:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "sync_key": "sync-key-empty-001",
                    "sync_continue_flag": 0,
                    "last_message_sync_count": 2,
                    "last_message_sync_ingested": 0,
                    "last_message_sync_deduplicated": 2,
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel)

        diagnostics = overview["sync_diagnostics"]
        self.assertEqual(diagnostics["state"], "healthy")
        self.assertEqual(diagnostics["state_label"], "最近同步成功但无新增")
        self.assertIn("本轮没有新增入库消息", diagnostics["operator_hint"])
        self.assertIn("分类=最近同步成功但无新增", diagnostics["operator_summary"])
        self.assertIn("拉取/新增/去重=2/0/2", diagnostics["operator_summary"])
        self.assertIn("continue_flag=已到末尾 (0)", diagnostics["operator_summary"])
        self.assertEqual(diagnostics["operator_snapshot"]["status_key"], "success_no_new")
        self.assertEqual(diagnostics["operator_snapshot"]["status_label"], "最近同步成功但无新增")
        self.assertIn("空拉取或重复消息回放", diagnostics["operator_snapshot"]["summary"])

    def test_wechat_ipad_overview_marks_sync_diagnostics_pending_continue_and_progressing(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.last_message_sync_at = datetime.fromisoformat("2026-03-23T12:05:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "sync_key": "sync-key-continue-001",
                    "sync_continue_flag": 1,
                    "last_message_sync_count": 8,
                    "last_message_sync_ingested": 4,
                    "last_message_sync_deduplicated": 4,
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            pending_overview = build_wechat_ipad_overview(channel)

            channel.last_message_sync_at = datetime.fromisoformat("2026-03-23T12:15:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "sync_key": "sync-key-progress-001",
                    "sync_continue_flag": 0,
                    "last_message_sync_count": 6,
                    "last_message_sync_ingested": 5,
                    "last_message_sync_deduplicated": 1,
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            progressing_overview = build_wechat_ipad_overview(channel)

        pending_snapshot = pending_overview["sync_diagnostics"]["operator_snapshot"]
        self.assertEqual(pending_snapshot["status_key"], "pending_continue")
        self.assertEqual(pending_snapshot["status_label"], "待续拉取")
        self.assertIn("continue_flag=1", pending_snapshot["summary"])
        self.assertIn("最近同步/新增/去重=8/4/4", pending_snapshot["detail"])

        progressing_diagnostics = progressing_overview["sync_diagnostics"]
        progressing_snapshot = progressing_diagnostics["operator_snapshot"]
        self.assertEqual(progressing_diagnostics["state_label"], "最近同步正常推进")
        self.assertEqual(progressing_snapshot["status_key"], "progressing")
        self.assertEqual(progressing_snapshot["status_tone"], "success")
        self.assertIn("已有新增或持续推进迹象", progressing_snapshot["summary"])
        self.assertIn("最近同步/新增/去重=6/5/1", progressing_snapshot["detail"])

    def test_wechat_ipad_channel_detail_renders_sync_operator_snapshot_card(self):
        with SessionLocal() as db:
            admin = db.scalar(select(AdminUser).where(AdminUser.username == "admin"))
            self.assertIsNotNone(admin)
            self.client.cookies.set(AUTH_COOKIE_NAME, build_auth_cookie(admin))
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.last_message_sync_at = datetime.fromisoformat("2026-03-23T12:15:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "sync_key": "sync-key-progress-001",
                    "sync_continue_flag": 0,
                    "last_message_sync_count": 6,
                    "last_message_sync_ingested": 5,
                    "last_message_sync_deduplicated": 1,
                },
                ensure_ascii=False,
            )
            db.commit()

        response = self.client.get("/channels/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("当前同步档位：最近同步正常推进", response.text)
        self.assertIn("最近同步链路已有新增或持续推进迹象", response.text)
        self.assertIn("最近同步/新增/去重=6/5/1", response.text)

    def test_wechat_ipad_overview_builds_recent_message_flow_summary(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_receive_at": "2026-03-24T11:57:00+00:00",
                    "last_receive_event_key": "evt-receive-001",
                    "last_receive_summary": "收到 wechat_ipad_sync 事件，event_key=evt-receive-001，已入处理链路。",
                    "last_inbound_protocol_summary": {
                        "source": "wechat_ipad_sync",
                        "event_key": "evt-receive-001",
                        "mapped": True,
                        "deduplicated": False,
                    },
                    "last_send_at": "2026-03-24T11:55:30+00:00",
                    "last_send_summary": "发送 text 到 room-ops，回执 delivered。",
                    "last_outbound_protocol_summary": {
                        "receiver_id": "room-ops",
                        "external_message_id": "msg-out-001",
                        "endpoint": "/api/SendMsg",
                    },
                    "receive_total": 8,
                    "dedupe_total": 3,
                    "last_dedupe_event_key": "evt-dedupe-001",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        summary = overview["recent_message_flow"]
        self.assertEqual(summary["status_key"], "both_active")
        self.assertEqual(summary["status_label"], "最近收发均有样本")
        self.assertIn("最近接收与发送链路都已有样本", summary["summary"])
        self.assertIn("优先关注=双侧都正常", summary["detail"])
        self.assertIn("累计接收/去重=8/3", summary["detail"])
        self.assertIn("最近去重事件=evt-dedupe-001", summary["detail"])
        self.assertIn("分类=最近收发均有样本", summary["operator_summary"])
        self.assertIn("优先关注=双侧都正常", summary["operator_summary"])
        self.assertEqual(summary["freshness_label"], "双向新鲜")
        self.assertEqual(summary["attention_label"], "持续观察")
        self.assertIn("最近接收和发送都是新鲜样本", summary["attention_action"])
        self.assertEqual(summary["operator_snapshot"]["status_label"], "最近收发均有样本")
        self.assertEqual(summary["operator_snapshot"]["focus_label"], "持续观察")
        self.assertEqual(summary["receive"]["judgement_label"], "接收正常")
        self.assertEqual(summary["send"]["judgement_label"], "发送正常")
        self.assertEqual(summary["side_by_side_summary"], "接收：接收正常（新鲜）；发送：发送正常（新鲜）")
        self.assertEqual(summary["priority_side_summary"], "当前拖后腿侧：双侧都正常；接收和发送两侧当前都没有明显拖后腿迹象。")
        self.assertEqual(summary["operator_snapshot"]["focus_side_label"], "双侧都正常")
        self.assertIn("优先关注=双侧都正常", summary["operator_snapshot"]["evidence"])
        self.assertIn("接收=接收正常 新鲜 2026-03-24 19:57:00 CST", summary["operator_snapshot"]["evidence"])
        self.assertIn("发送=发送正常 新鲜 2026-03-24 19:55:30 CST", summary["operator_snapshot"]["evidence"])
        self.assertIn("当前判断=最近收发均有样本", summary["operator_snapshot"]["operator_summary"])
        self.assertIn("接收判断=接收正常", summary["operator_snapshot"]["operator_summary"])
        self.assertIn("发送判断=发送正常", summary["operator_snapshot"]["operator_summary"])
        self.assertIn("下一步=最近接收和发送都是新鲜样本", summary["operator_snapshot"]["operator_summary"])
        self.assertEqual(summary["receive"]["state_label"], "有样本")
        self.assertEqual(summary["receive"]["reference"], "wechat_ipad_sync")
        self.assertEqual(summary["receive"]["freshness_label"], "新鲜")
        self.assertEqual(summary["receive"]["ops_brief"], "接收正常 / 新鲜 / 2026-03-24 19:57:00 CST")
        self.assertIn("摘要=收到 wechat_ipad_sync 事件", summary["receive"]["operator_summary"])
        self.assertEqual(summary["send"]["state_label"], "有样本")
        self.assertEqual(summary["send"]["reference"], "room-ops")
        self.assertEqual(summary["send"]["freshness_label"], "新鲜")
        self.assertEqual(summary["send"]["ops_brief"], "发送正常 / 新鲜 / 2026-03-24 19:55:30 CST")
        self.assertIn("摘要=发送 text 到 room-ops", summary["send"]["operator_summary"])

    def test_wechat_ipad_channel_detail_renders_recent_message_flow_card(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.asc())).all()
            channel.runtime_info_json = json.dumps(
                {
                    "last_receive_at": "2026-03-24T11:57:00+00:00",
                    "last_receive_event_key": "evt-receive-001",
                    "last_receive_summary": "收到 wechat_ipad_sync 事件，event_key=evt-receive-001，已入处理链路。",
                    "last_inbound_protocol_summary": {
                        "source": "wechat_ipad_sync",
                        "event_key": "evt-receive-001",
                    },
                    "last_send_at": "2026-03-24T11:55:30+00:00",
                    "last_send_summary": "发送 text 到 room-ops，回执 delivered。",
                    "last_outbound_protocol_summary": {
                        "receiver_id": "room-ops",
                    },
                    "receive_total": 8,
                    "dedupe_total": 3,
                    "last_dedupe_event_key": "evt-dedupe-001",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            overview = build_wechat_ipad_overview(channel)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=overview,
                wechat_ipad_group_summary=None,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("最近收发摘要：最近收发均有样本", html)
        self.assertIn("最近接收与发送链路都已有样本", html)
        self.assertIn("累计接收/去重=8/3", html)
        self.assertIn("双向新鲜", html)
        self.assertIn("持续观察", html)
        self.assertIn("最近接收和发送都是新鲜样本", html)
        self.assertIn("分侧判断：接收：接收正常（新鲜）；发送：发送正常（新鲜）", html)
        self.assertIn("关键证据：优先关注=双侧都正常", html)
        self.assertIn("接收正常", html)
        self.assertIn("发送正常", html)
        self.assertIn("收发分侧判断", html)
        self.assertIn("拖后腿侧", html)
        self.assertIn("收发优先关注", html)
        self.assertIn("接收一眼判断", html)
        self.assertIn("发送一眼判断", html)
        self.assertIn("接收判断", html)
        self.assertIn("发送判断", html)
        self.assertIn("当前判断=最近收发均有样本", html)

    def test_wechat_ipad_overview_marks_recent_message_flow_missing_send_sample(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_receive_at": "2026-03-24T11:20:00+00:00",
                    "last_receive_event_key": "evt-receive-only-001",
                    "last_receive_summary": "收到 inbound 回调，已完成标准化。",
                    "receive_total": 2,
                    "dedupe_total": 1,
                    "last_dedupe_event_key": "evt-dedupe-only-001",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        summary = overview["recent_message_flow"]
        self.assertEqual(summary["status_key"], "receive_only")
        self.assertEqual(summary["freshness_label"], "单侧缺样本")
        self.assertEqual(summary["attention_label"], "先补发送样本")
        self.assertIn("缺少最近发送样本", summary["attention_action"])
        self.assertEqual(summary["focus_side"], "send")
        self.assertEqual(summary["focus_side_label"], "发送缺样本")
        self.assertEqual(summary["side_by_side_summary"], "接收：接收样本偏旧（偏旧）；发送：发送缺样本（尚无时间样本）")
        self.assertEqual(summary["priority_side_summary"], "当前拖后腿侧：发送缺样本；发送侧还没有最近样本，当前无法确认这侧链路是否真的在跑。")
        self.assertEqual(summary["receive"]["judgement_label"], "接收样本偏旧")
        self.assertEqual(summary["send"]["judgement_label"], "发送缺样本")
        self.assertEqual(summary["send"]["ops_brief"], "发送缺样本 / 尚无时间样本 / -")
        self.assertEqual(summary["operator_snapshot"]["items"][0]["label"], "发送")
        self.assertIn("当前更值得先处理发送缺样本", summary["summary"])
        self.assertIn("优先关注=发送缺样本", summary["detail"])
        self.assertEqual(summary["send"]["state_label"], "暂无样本")
        self.assertEqual(summary["send"]["freshness_label"], "尚无时间样本")

    def test_wechat_ipad_overview_prioritizes_staler_send_side_in_recent_message_flow(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_receive_at": "2026-03-24T11:56:00+00:00",
                    "last_receive_event_key": "evt-receive-healthy-001",
                    "last_receive_summary": "收到 inbound 回调，已完成标准化。",
                    "last_send_at": "2026-03-24T10:20:00+00:00",
                    "last_send_summary": "发送 text 到 room-ops，回执 delivered。",
                    "last_outbound_protocol_summary": {
                        "receiver_id": "room-ops",
                    },
                    "receive_total": 6,
                    "dedupe_total": 1,
                    "last_dedupe_event_key": "evt-dedupe-priority-001",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        summary = overview["recent_message_flow"]
        self.assertEqual(summary["status_key"], "both_active")
        self.assertEqual(summary["focus_side"], "send")
        self.assertEqual(summary["focus_side_label"], "发送样本过旧")
        self.assertEqual(summary["attention_label"], "先刷新发送样本")
        self.assertEqual(summary["freshness_label"], "发送侧过旧")
        self.assertEqual(summary["priority_side_summary"], "当前拖后腿侧：发送样本过旧；发送侧样本已过旧，建议优先刷新这一侧。")
        self.assertIn("当前更值得先处理发送样本过旧", summary["summary"])
        self.assertIn("优先关注=发送样本过旧", summary["operator_snapshot"]["evidence"])
        self.assertEqual(summary["operator_snapshot"]["items"][0]["judgement_label"], "发送样本过旧")

    def test_wechat_ipad_channel_detail_surfaces_side_specific_message_flow_priority(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.asc())).all()
            channel.runtime_info_json = json.dumps(
                {
                    "last_receive_at": "2026-03-24T11:56:00+00:00",
                    "last_receive_event_key": "evt-receive-focus-001",
                    "last_receive_summary": "收到 inbound 回调，已完成标准化。",
                    "last_inbound_protocol_summary": {
                        "source": "wechat_ipad_sync",
                    },
                    "receive_total": 6,
                    "dedupe_total": 1,
                    "last_dedupe_event_key": "evt-dedupe-focus-001",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            overview = build_wechat_ipad_overview(channel, now=datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc))
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=overview,
                wechat_ipad_group_summary=None,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("分侧判断：接收：接收正常（新鲜）；发送：发送缺样本（尚无时间样本）", html)
        self.assertIn("当前拖后腿侧：发送缺样本", html)
        self.assertIn("发送一眼判断", html)
        self.assertIn("发送缺样本 / 尚无时间样本 / -", html)

    def test_wechat_ipad_overview_exposes_structured_recent_protocol_results(self):
        reference_time = datetime(2026, 3, 23, 12, 37, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-23T12:34:56+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "login_qr_message": "扫码中",
                    "last_check_qr_at": "2026-03-23T12:30:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                    "heartbeat_status": "ok",
                    "heartbeat_long_status": "ok",
                    "last_heartbeat_long_at": "2026-03-23T12:35:30+00:00",
                    "last_heartbeat_response": {"Ret": 0, "Msg": "heartbeat ok", "http_status": 200},
                    "last_heartbeat_long_response": {"Ret": 0, "Msg": "heartbeat long ok", "http_status": 200},
                    "last_cache_info_at": "2026-03-23T12:36:00+00:00",
                    "last_cache_info_response": {
                        "Ret": 0,
                        "Data": {
                            "Wxid": "wxid_runtime_structured",
                            "NickName": "结构化运行号",
                            "DeviceID": "device-structured-001",
                        },
                        "http_status": 200,
                    },
                    "cache_info_summary": {
                        "wxid": "wxid_runtime_structured",
                        "nickname": "结构化运行号",
                        "device_id": "device-structured-001",
                    },
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        protocol_state = overview["protocol_state"]
        self.assertEqual(protocol_state["last_check_qr_result"]["state_label"], "调用成功")
        self.assertEqual(protocol_state["last_check_qr_result"]["freshness_label"], "新鲜")
        self.assertEqual(protocol_state["last_check_qr_result"]["message"], "等待扫码")
        self.assertEqual(protocol_state["last_check_qr_result"]["http_status"], "200")
        self.assertEqual(protocol_state["last_check_qr_result"]["state_tone"], "success")
        self.assertEqual(protocol_state["last_check_qr_result"]["called_at_display"], "2026-03-23 20:30:00 CST")
        self.assertIn("分档=调用成功", protocol_state["last_check_qr_result"]["operator_summary"])
        self.assertIn("时效=新鲜", protocol_state["last_check_qr_result"]["operator_summary"])
        self.assertIn("最近调用=2026-03-23 20:30:00 CST", protocol_state["last_check_qr_result"]["operator_summary"])
        self.assertEqual(protocol_state["last_heartbeat_result"]["state_label"], "调用成功")
        self.assertEqual(protocol_state["last_heartbeat_result"]["freshness_label"], "新鲜")
        self.assertEqual(protocol_state["last_heartbeat_result"]["message"], "heartbeat ok")
        self.assertEqual(protocol_state["last_heartbeat_result"]["called_at_display"], "2026-03-23 20:34:56 CST")
        self.assertIn("消息=heartbeat ok", protocol_state["last_heartbeat_result"]["operator_summary"])
        self.assertEqual(protocol_state["last_heartbeat_long_result"]["message"], "heartbeat long ok")
        self.assertEqual(protocol_state["last_heartbeat_long_result"]["called_at_display"], "2026-03-23 20:35:30 CST")
        self.assertEqual(protocol_state["last_cache_info_result"]["state_label"], "调用成功")
        self.assertEqual(protocol_state["last_cache_info_result"]["freshness_label"], "新鲜")
        self.assertEqual(protocol_state["last_cache_info_result"]["called_at_display"], "2026-03-23 20:36:00 CST")
        self.assertEqual(protocol_state["last_cache_info_result"]["wxid"], "wxid_runtime_structured")
        self.assertEqual(protocol_state["last_cache_info_result"]["nickname"], "结构化运行号")
        self.assertEqual(protocol_state["last_cache_info_result"]["device_id"], "device-structured-001")
        self.assertIn("时效=新鲜", protocol_state["last_cache_info_result"]["operator_summary"])
        self.assertIn("最近调用=2026-03-23 20:36:00 CST", protocol_state["last_cache_info_result"]["operator_summary"])
        self.assertIn("wxid=wxid_runtime_structured", protocol_state["last_cache_info_result"]["operator_summary"])

    def test_wechat_ipad_overview_builds_login_operator_snapshot(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.login_state = "qrcode_ready"
            channel.last_login_at = datetime.fromisoformat("2026-03-24T11:20:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "qrcode_expires_at": "2026-03-24T11:40:00+00:00",
                    "qrcode_refresh_count": 2,
                    "login_qr_message": "二维码已过期，请刷新后重试",
                    "last_check_qr_at": "2026-03-24T11:38:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        snapshot = overview["login_operator_snapshot"]
        self.assertEqual(snapshot["status_key"], "expired")
        self.assertEqual(snapshot["status_label"], "二维码已过期")
        self.assertEqual(snapshot["attention_label"], "刷新二维码")
        self.assertIn("当前二维码样本已过期", snapshot["summary"])
        self.assertIn("最近登录=2026-03-24 19:20:00 CST", snapshot["detail"])
        self.assertIn("最近 CheckQR=2026-03-24 19:38:00 CST", snapshot["detail"])
        self.assertIn("CheckQR 消息=等待扫码", snapshot["detail"])
        self.assertIn("分类=二维码已过期", snapshot["operator_summary"])
        self.assertIn("二维码=expired", snapshot["operator_summary"])
        self.assertIn("下一步=先执行“刷新二维码”", snapshot["operator_summary"])

    def test_wechat_ipad_channel_detail_renders_login_operator_snapshot_card(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.asc())).all()
            channel.login_state = "qrcode_ready"
            channel.qrcode_status = "waiting_scan"
            channel.runtime_info_json = json.dumps(
                {
                    "qrcode_expires_at": "2026-03-24T12:20:00+00:00",
                    "qrcode_refresh_count": 1,
                    "login_qr_message": "等待扫码",
                    "last_check_qr_at": "2026-03-24T11:58:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            overview = build_wechat_ipad_overview(channel, now=reference_time)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=overview,
                wechat_ipad_group_summary=None,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("登录 / 二维码一眼判断：等待扫码", html)
        self.assertIn("当前已有可用二维码链路，下一眼先判断二维码是否还有效、CheckQR 是否持续更新。", html)
        self.assertIn("先扫码；若运维侧已扫码但页面仍不推进，马上核对最近 CheckQR 时间和返回消息。", html)
        self.assertIn("登录态qrcode_ready", html)
        self.assertIn("二维码状态waiting_scan", html)
        self.assertIn("CheckQR调用成功 / 新鲜", html)

    def test_wechat_ipad_overview_marks_recent_protocol_results_warning_and_empty_states(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "login_qr_message": "等待扫码确认",
                    "last_heartbeat_response": {"Ret": 1, "Msg": "heartbeat down", "http_status": 500},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel)

        protocol_state = overview["protocol_state"]
        self.assertEqual(protocol_state["last_check_qr_result"]["state_label"], "等待结果")
        self.assertIn("等待扫码确认", protocol_state["last_check_qr_result"]["operator_hint"])
        self.assertEqual(protocol_state["last_heartbeat_result"]["state_label"], "调用失败")
        self.assertIn("heartbeat down", protocol_state["last_heartbeat_result"]["operator_hint"])
        self.assertEqual(protocol_state["last_cache_info_result"]["state_label"], "尚未调用")
        self.assertEqual(protocol_state["last_cache_info_result"]["freshness_label"], "尚无时间样本")
        self.assertIn("还没有最近调用记录", protocol_state["last_cache_info_result"]["operator_hint"])
        self.assertIn("尚无时间样本", protocol_state["last_cache_info_result"]["operator_hint"])

    def test_wechat_ipad_overview_marks_recent_protocol_results_blank_summary_state(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_check_qr_response": {"Ret": 0, "http_status": 200},
                    "last_heartbeat_response": {"Ret": 0, "http_status": 200},
                    "last_cache_info_response": {"Ret": 0, "Data": {}, "http_status": 200},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel)

        protocol_state = overview["protocol_state"]
        self.assertEqual(protocol_state["last_check_qr_result"]["state_label"], "返回空摘要")
        self.assertEqual(protocol_state["last_check_qr_result"]["state_tone"], "outline")
        self.assertIn("没有硬错误", protocol_state["last_check_qr_result"]["operator_hint"])
        self.assertEqual(protocol_state["last_heartbeat_result"]["state_label"], "返回空摘要")
        self.assertIn("分档=返回空摘要", protocol_state["last_heartbeat_result"]["operator_summary"])
        self.assertEqual(protocol_state["last_cache_info_result"]["state_label"], "返回空摘要")
        self.assertIn("wxid=-", protocol_state["last_cache_info_result"]["operator_summary"])

    def test_wechat_ipad_overview_marks_recent_protocol_results_overdue(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_check_qr_at": "2026-03-24T10:30:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        result = overview["protocol_state"]["last_check_qr_result"]
        self.assertEqual(result["freshness_label"], "过旧")
        self.assertEqual(result["freshness_key"], "expired")
        self.assertIn("时效=过旧", result["operator_summary"])
        self.assertIn("结果已过旧", result["operator_hint"])


    def test_runtime_schema_creates_chatroom_tables_for_wechat_ipad(self):
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS chatroom_members"))
            connection.execute(text("DROP TABLE IF EXISTS chatrooms"))

        from app.services.runtime import ensure_runtime_schema

        ensure_runtime_schema()

        inspector = inspect(engine)
        self.assertIn("chatrooms", inspector.get_table_names())
        self.assertIn("chatroom_members", inspector.get_table_names())

    def test_wechat_ipad_group_summary_reads_local_chatroom_entities(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-001@chatroom",
                name="运维群",
                remark="核心联调",
                owner_external_id="wxid_owner_001",
                status="active",
                member_count=2,
                extra_json=json.dumps({"source": "local-seed"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add_all(
                [
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_member_001",
                        nickname="张三",
                        display_name="张三-群名片",
                        role="owner",
                    ),
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_member_002",
                        nickname="李四",
                        display_name="李四-群名片",
                        role="member",
                    ),
                ]
            )
            db.commit()
            summary = build_wechat_ipad_group_summary(db, channel)

        self.assertEqual(summary["chatroom_count"], 1)
        self.assertEqual(summary["member_count"], 2)
        self.assertEqual(summary["member_synced_chatroom_count"], 1)
        self.assertEqual(summary["latest_chatroom_name"], "运维群")
        self.assertEqual(summary["latest_observed_source"], "local-seed")
        self.assertEqual(summary["message_observed_count"], 0)
        self.assertEqual(summary["active_chatroom_count"], 1)
        self.assertEqual(len(summary["recent_chatrooms"]), 1)
        self.assertEqual(summary["recent_chatrooms"][0]["status_label"], "已补充资料")
        self.assertEqual(summary["sync_depth"]["status_key"], "member_records_available")
        self.assertEqual(summary["sync_depth"]["status_label"], "已有成员记录")
        self.assertIn("1 个群已有成员记录", summary["sync_depth"]["operator_summary"])
        self.assertEqual(summary["visibility_snapshot"]["summary_label"], "已有成员记录")
        self.assertEqual(summary["visibility_snapshot"]["status_tone"], "success")
        self.assertEqual(summary["visibility_snapshot"]["recent_sources"], ["local-seed"])
        self.assertIn("群资料覆盖 100%", summary["coverage_snapshot"]["summary_label"])
        self.assertEqual(summary["coverage_snapshot"]["items"][0]["value_label"], "1 / 1")
        self.assertEqual(summary["coverage_snapshot"]["items"][0]["percent_label"], "100%")
        self.assertEqual(summary["coverage_snapshot"]["items"][1]["label"], "成员记录覆盖")
        detail_map = {item["label"]: item["value"] for item in summary["coverage_snapshot"]["detail_items"]}
        self.assertEqual(detail_map["最近观测来源"], "local-seed")
        self.assertEqual(detail_map["最近同步时效"], "尚无时间样本")
        self.assertEqual(summary["ops_snapshot"]["status_key"], "member_records_available")
        self.assertEqual(summary["ops_snapshot"]["headline"], "当前群资料同步判断：已有成员记录")
        self.assertIn("1 个群已有成员记录", summary["ops_snapshot"]["summary"])
        self.assertIn("进入更细的群运维排查", summary["ops_snapshot"]["action"])
        self.assertEqual(summary["stage_snapshot"]["status_key"], "member_records_available")
        self.assertEqual(summary["stage_snapshot"]["headline"], "群资料同步一眼判断：已有成员记录")
        self.assertIn("1 个群已有成员记录", summary["stage_snapshot"]["operator_summary"])
        self.assertIn("下一步：当前可继续核对最近来源和成员覆盖", summary["stage_snapshot"]["operator_summary"])
        self.assertEqual(summary["plugin_scope_snapshot"]["stage_key"], "ready_for_modeling")
        self.assertEqual(summary["plugin_scope_snapshot"]["stage_label"], "可进入群级权限建模")
        self.assertIn("1 个群已有成员记录", summary["plugin_scope_snapshot"]["evidence"])
        self.assertIn("allow/deny", summary["plugin_scope_snapshot"]["action"])
        self.assertIn("下一步：可开始设计群级启停 / allow/deny / 能力边界的数据口径", summary["plugin_scope_snapshot"]["operator_summary"])
        self.assertIn("当前仅为群级插件权限治理的准备度/扩展位", summary["plugin_scope_snapshot"]["boundary_note"])
        self.assertEqual(summary["plugin_scope_snapshot"]["readiness_summary"], "前置条件：群资料=1，成员记录群=1，bot 插件绑定启用/总计=3/4，插件镜像=4。")
        self.assertEqual(
            summary["plugin_scope_snapshot"]["preconditions"],
            [
                {
                    "label": "群资料实体",
                    "value": "1",
                    "state": "ready",
                    "summary": "本地 chatrooms 已有 1 个样本",
                },
                {
                    "label": "成员记录群",
                    "value": "1",
                    "state": "ready",
                    "summary": "已有 1 个群具备 chatroom_members",
                },
                {
                    "label": "bot 插件绑定",
                    "value": "3 / 4",
                    "state": "ready",
                    "summary": "bot 插件绑定已存在，启用 3 / 总计 4",
                },
            ],
        )
        self.assertEqual(
            summary["plugin_scope_snapshot"]["missing_capabilities"],
            [
                "缺群级 enable/disable 映射",
                "缺 allow/deny 持久化口径",
                "缺群级命令/能力边界模型",
                "缺审计与运营动作留痕",
            ],
        )
        self.assertEqual(
            summary["plugin_scope_brief"]["focus_gap"],
            "仍缺 enable/disable 映射、allow/deny 持久化口径、命令/能力边界模型、审计与运营动作留痕；当前可进入最小规则建模。",
        )
        self.assertEqual(
            summary["plugin_scope_brief"]["missing_summary"],
            "主缺enable/disable 映射、allow/deny 持久化口径、命令/能力边界模型、审计与运营动作留痕四类核心模型；当前可进入最小规则建模。",
        )
        self.assertEqual(summary["plugin_scope_brief"]["modeling_tier_label"], "已具备建模前提")
        self.assertEqual(summary["plugin_scope_brief"]["modeling_tier_tone"], "success")
        self.assertIn("可直接进入群级治理规则收口", summary["plugin_scope_brief"]["modeling_tier_summary"])
        self.assertEqual(summary["plugin_scope_brief"]["primary_gap_label"], "群级启停模型")
        self.assertIn("否则无法落最小开关治理", summary["plugin_scope_brief"]["primary_gap_summary"])
        self.assertEqual(summary["plugin_scope_brief"]["blocker_family_label"], "模型前置")
        self.assertIn("先补群级启停模型", summary["plugin_scope_brief"]["blocker_short_summary"])
        self.assertIn("已具备建模前提；主 blocker：群级启停模型；下一步：可进入群级权限建模", summary["plugin_scope_brief"]["decision_summary"])
        self.assertEqual(summary["plugin_scope_overview"]["headline"], "群级权限治理摘要：已具备建模前提")
        self.assertEqual(summary["plugin_scope_overview"]["priority_label"], "中")
        self.assertEqual(summary["plugin_scope_overview"]["primary_gap_label"], "群级启停模型")
        self.assertEqual(summary["plugin_scope_overview"]["blocker_family_label"], "模型前置")
        self.assertIn("模型前置 / 群级启停模型：", summary["plugin_scope_overview"]["blocker_snapshot_summary"])
        self.assertIn("已具备建模前提；主 blocker：群级启停模型；下一步：可进入群级权限建模", summary["plugin_scope_overview"]["summary"])
        self.assertIn("主 blocker=群级启停模型", summary["plugin_scope_overview"]["operator_summary"])
        self.assertIn("当前档位：可进入群级权限建模；建模判断：已具备建模前提；优先级：中；主 blocker：群级启停模型；下一步：可进入群级权限建模", summary["plugin_scope_overview"]["single_overview_summary"])
        self.assertEqual(summary["group_enablement_model_snapshot"]["status_label"], "已定义草案级口径")
        self.assertTrue(summary["group_enablement_model_snapshot"]["is_defined"])
        self.assertEqual(
            summary["group_enablement_model_snapshot"]["recommended_primary_key"],
            "channel_id + chatroom_id(或 chatroom_external_id) + bot_plugin_id",
        )
        self.assertEqual(
            [item["key"] for item in summary["group_enablement_model_snapshot"]["primary_key_dimensions"]],
            ["channel_id", "chatroom_id_or_external_id", "bot_plugin_id", "plugin_projection"],
        )
        self.assertIn(
            "channel -> chatroom -> bot_plugin 为最小控制链路",
            summary["group_enablement_model_snapshot"]["relationship_scope_summary"],
        )
        self.assertIn(
            "不代表真实群级启停控制、写入接口或权限系统已经上线",
            summary["group_enablement_model_snapshot"]["boundary_note"],
        )
        self.assertEqual(summary["group_allow_deny_model_snapshot"]["status_label"], "已定义草案级口径")
        self.assertTrue(summary["group_allow_deny_model_snapshot"]["is_defined"])
        self.assertFalse(summary["group_allow_deny_model_snapshot"]["is_live"])
        self.assertEqual(
            summary["group_allow_deny_model_snapshot"]["recommended_dimension_keys"],
            [
                "channel_id",
                "chatroom_id_or_external_id",
                "bot_plugin_id",
                "action_scope",
                "policy_effect",
            ],
        )
        self.assertIn(
            "action_scope + policy_effect",
            summary["group_allow_deny_model_snapshot"]["relationship_scope_summary"],
        )
        self.assertIn(
            "当前仅定义群级 allow/deny 口径摘要与扩展位",
            summary["group_allow_deny_model_snapshot"]["boundary_note"],
        )
        self.assertEqual(summary["group_plugin_governance_snapshot"]["blocker_family_label"], "模型前置")
        self.assertIn("模型前置 / 群级启停模型：", summary["group_plugin_governance_snapshot"]["blocker_snapshot_summary"])
        self.assertEqual(
            summary["group_plugin_governance_snapshot"]["single_overview_summary"],
            summary["group_plugin_governance_snapshot"]["overview_summary"],
        )
        self.assertEqual(
            summary["group_plugin_governance_snapshot"]["sample_gate_snapshot"]["gate_label"],
            "样本门槛已满足",
        )
        self.assertIn(
            "当前本地 chatroom/member 样本已具备最小门槛",
            summary["group_plugin_governance_snapshot"]["sample_gate_snapshot"]["summary"],
        )
        self.assertIn(
            "前置条件：群资料=1，成员记录群=1，bot 插件绑定启用/总计=3/4，插件镜像=4；样本时效=尚无时间样本。",
            summary["group_plugin_governance_snapshot"]["sample_gate_snapshot"]["evidence"],
        )
        self.assertEqual(summary["operator_snapshot"]["status_key"], "member_records_available")
        self.assertEqual(summary["operator_snapshot"]["headline"], "当前群资料同步档位：已有成员记录")
        self.assertIn("当前摘要未发现 message_observed 骨架", summary["operator_snapshot"]["detail"])
        self.assertTrue(summary["data_model_ready"])

    def test_wechat_ipad_group_summary_handles_empty_chatroom_state(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            summary = build_wechat_ipad_group_summary(db, channel)

        self.assertEqual(summary["chatroom_count"], 0)
        self.assertEqual(summary["member_count"], 0)
        self.assertEqual(summary["member_synced_chatroom_count"], 0)
        self.assertEqual(summary["latest_observed_source"], "-")
        self.assertEqual(summary["recent_chatrooms"], [])
        self.assertFalse(summary["has_chatroom_data"])
        self.assertEqual(summary["sync_depth"]["status_key"], "no_sample")
        self.assertEqual(summary["sync_depth"]["status_label"], "尚未形成样本")
        self.assertEqual(summary["visibility_snapshot"]["summary_label"], "尚未形成样本")
        self.assertEqual(summary["visibility_snapshot"]["recent_sources"], [])
        self.assertEqual(summary["coverage_snapshot"]["summary_label"], "暂无群资料样本")
        self.assertEqual(summary["coverage_snapshot"]["items"][0]["value_label"], "0 / 0")
        self.assertEqual(summary["coverage_snapshot"]["items"][2]["percent_label"], "0%")
        self.assertEqual(summary["ops_snapshot"]["status_key"], "no_sample")
        self.assertIn("当前没有本地 chatroom 或 chatroom_members 样本", summary["ops_snapshot"]["evidence"])
        self.assertIn("等待首批群消息或群资料同步落库", summary["ops_snapshot"]["action"])
        self.assertEqual(summary["stage_snapshot"]["status_key"], "no_sample")
        self.assertIn("尚未形成样本；最近同步时效：尚无时间样本；当前没有本地 chatroom 或 chatroom_members 样本", summary["stage_snapshot"]["operator_summary"])
        self.assertIn("下一步：先等待首批群消息或群资料同步落库，再判断是否进入下一档。", summary["stage_snapshot"]["operator_summary"])
        self.assertEqual(summary["plugin_scope_snapshot"]["stage_key"], "not_ready")
        self.assertEqual(summary["plugin_scope_snapshot"]["stage_label"], "尚未具备")
        self.assertIn("群级插件权限仍停留在扩展位骨架", summary["plugin_scope_snapshot"]["evidence"])
        self.assertIn("先补首批群消息或群资料样本", summary["plugin_scope_snapshot"]["action"])
        self.assertIn("尚未具备；当前没有本地 chatroom / chatroom_members 样本", summary["plugin_scope_snapshot"]["operator_summary"])
        self.assertEqual(
            summary["plugin_scope_brief"]["focus_gap"],
            "仍缺 enable/disable 映射、allow/deny 持久化口径、命令/能力边界模型、审计与运营动作留痕；当前先补群资料样本。",
        )
        self.assertEqual(
            summary["plugin_scope_brief"]["missing_summary"],
            "主缺enable/disable 映射、allow/deny 持久化口径、命令/能力边界模型、审计与运营动作留痕四类核心模型；当前先补群资料样本。",
        )
        self.assertEqual(summary["plugin_scope_brief"]["modeling_tier_label"], "仍不可建模")
        self.assertEqual(summary["plugin_scope_brief"]["modeling_tier_tone"], "muted")
        self.assertIn("群级权限治理还不适合进入建模", summary["plugin_scope_brief"]["modeling_tier_summary"])
        self.assertEqual(summary["plugin_scope_brief"]["primary_gap_label"], "群资料样本")
        self.assertIn("群资料样本不足", summary["plugin_scope_brief"]["primary_gap_summary"])
        self.assertEqual(summary["plugin_scope_brief"]["blocker_family_label"], "样本前置")
        self.assertIn("仍不可建模；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本", summary["plugin_scope_brief"]["decision_summary"])
        self.assertEqual(summary["plugin_scope_overview"]["headline"], "群级权限治理摘要：仍不可建模")
        self.assertEqual(summary["plugin_scope_overview"]["priority_label"], "高")
        self.assertEqual(summary["plugin_scope_overview"]["primary_gap_label"], "群资料样本")
        self.assertEqual(summary["plugin_scope_overview"]["blocker_family_label"], "样本前置")
        self.assertIn("样本前置 / 群资料样本：", summary["plugin_scope_overview"]["blocker_snapshot_summary"])
        self.assertIn("仍不可建模；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本", summary["plugin_scope_overview"]["summary"])
        self.assertIn("主 blocker=群资料样本", summary["plugin_scope_overview"]["operator_summary"])
        self.assertIn("当前档位：尚未具备；建模判断：仍不可建模；优先级：高；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本", summary["plugin_scope_overview"]["single_overview_summary"])
        self.assertEqual(summary["group_enablement_model_snapshot"]["status_label"], "已定义草案级口径")
        self.assertEqual(summary["group_enablement_model_snapshot"]["current_stage_label"], "尚未具备")
        self.assertEqual(summary["group_enablement_model_snapshot"]["primary_gap_label"], "群资料样本")
        self.assertIn(
            "建议主键=channel_id + chatroom_id(或 external_id) + bot_plugin_id",
            summary["group_enablement_model_snapshot"]["operator_summary"],
        )
        self.assertEqual(summary["group_allow_deny_model_snapshot"]["status_label"], "已定义草案级口径")
        self.assertTrue(summary["group_allow_deny_model_snapshot"]["is_defined"])
        self.assertFalse(summary["group_allow_deny_model_snapshot"]["is_live"])
        self.assertEqual(
            summary["group_allow_deny_model_snapshot"]["recommended_dimension_keys"],
            [
                "channel_id",
                "chatroom_id_or_external_id",
                "bot_plugin_id",
                "action_scope",
                "policy_effect",
            ],
        )
        self.assertIn(
            "建议维度=channel_id + chatroom_id(或 external_id) + bot_plugin_id + action_scope + policy_effect",
            summary["group_allow_deny_model_snapshot"]["operator_summary"],
        )
        self.assertIn(
            "判定执行或权限系统已经上线",
            summary["group_allow_deny_model_snapshot"]["boundary_note"],
        )
        self.assertEqual(summary["operator_snapshot"]["status_key"], "no_sample")
        self.assertIn("当前还没有本地群资料样本", summary["operator_snapshot"]["detail"])
        self.assertIn("尚未观测到群资料", summary["empty_state_message"])
        self.assertEqual(summary["coverage_snapshot"]["sync_freshness"]["key"], "no_sample")
        self.assertEqual(summary["coverage_snapshot"]["sync_freshness"]["label"], "尚无时间样本")
        self.assertEqual(summary["ops_snapshot"]["freshness_label"], "尚无时间样本")

    def test_wechat_ipad_allow_deny_model_snapshot_helper_outputs_draft_summary(self):
        snapshot = wechat_ipad_service._build_group_allow_deny_model_snapshot(
            plugin_scope_snapshot={
                "stage_label": "可进入群级权限建模",
                "stage_tone": "success",
                "readiness_summary": "前置条件：群资料=1，成员记录群=1，bot 插件绑定启用/总计=3/4，插件镜像=4。",
                "action": "可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。",
            },
            plugin_scope_brief={
                "status_label": "可进入群级权限建模",
                "status_tone": "success",
                "next_step_summary": "可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。",
            },
        )

        self.assertEqual(snapshot["model_key"], "group_allow_deny_model_snapshot")
        self.assertEqual(snapshot["model_label"], "群级 allow/deny 口径")
        self.assertEqual(snapshot["status_label"], "已定义草案级口径")
        self.assertTrue(snapshot["is_defined"])
        self.assertFalse(snapshot["is_live"])
        self.assertEqual(snapshot["definition_level"], "draft")
        self.assertEqual(
            snapshot["recommended_dimension_keys"],
            ["channel_id", "chatroom_id_or_external_id", "bot_plugin_id", "action_scope", "policy_effect"],
        )
        self.assertIn("action_scope + policy_effect", snapshot["relationship_scope_summary"])
        self.assertIn("只读草案摘要", snapshot["summary"])
        self.assertIn("判定执行或权限系统已经上线", snapshot["boundary_note"])
        self.assertIn("policy_effect", snapshot["operator_summary"])

    def test_wechat_ipad_group_summary_marks_profile_without_members(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            db.add(
                Chatroom(
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    external_id="room-profile-only@chatroom",
                    name="资料已补群",
                    status="active",
                    member_count=8,
                    extra_json=json.dumps({"source": "manual_profile_sync"}, ensure_ascii=False),
                )
            )
            db.commit()
            summary = build_wechat_ipad_group_summary(db, channel)

        self.assertEqual(summary["sync_depth"]["status_key"], "profile_without_members")
        self.assertEqual(summary["sync_depth"]["status_label"], "已有群资料，无成员快照")
        self.assertIn("还没有成员快照", summary["sync_depth"]["operator_summary"])
        self.assertEqual(summary["visibility_snapshot"]["summary_label"], "已有群资料，无成员快照")
        self.assertEqual(summary["ops_snapshot"]["status_key"], "profile_without_members")
        self.assertIn("成员快照仍为 0", summary["ops_snapshot"]["evidence"])
        self.assertIn("优先补成员快照", summary["ops_snapshot"]["action"])
        self.assertEqual(summary["summary_snapshot"]["status_key"], "profile_without_members")
        self.assertEqual(summary["summary_snapshot"]["decision_label"], "待补成员快照")
        self.assertIn("档位=已有群资料，无成员快照", summary["summary_snapshot"]["operator_summary"])
        self.assertIn("判断=待补成员快照", summary["summary_snapshot"]["operator_summary"])
        self.assertEqual(summary["plugin_scope_snapshot"]["stage_key"], "profile_only")
        self.assertEqual(summary["plugin_scope_snapshot"]["stage_label"], "仅有群资料")
        self.assertIn("成员快照仍为 0", summary["plugin_scope_snapshot"]["evidence"])
        self.assertIn("下一步先补成员快照", summary["plugin_scope_snapshot"]["action"])
        self.assertEqual(summary["plugin_scope_brief"]["modeling_tier_label"], "仍不可建模")
        self.assertEqual(summary["plugin_scope_brief"]["primary_gap_label"], "群成员样本")
        self.assertEqual(summary["plugin_scope_brief"]["blocker_family_label"], "样本前置")
        self.assertIn("先补成员快照再谈规则建模", summary["plugin_scope_brief"]["primary_gap_summary"])
        self.assertIn("仍不可建模；主 blocker：群成员样本；下一步：下一步先补成员快照", summary["plugin_scope_brief"]["decision_summary"])

    def test_wechat_ipad_group_summary_marks_modelable_but_stale_when_samples_are_old(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-stale-ready@chatroom",
                name="样本偏旧群",
                status="active",
                member_count=6,
                last_synced_at=datetime.fromisoformat("2026-03-24T10:30:00+00:00"),
                extra_json=json.dumps({"source": "stale-ready-test"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add(
                ChatroomMember(
                    chatroom_id=chatroom.id,
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    member_external_id="wxid_member_stale_ready_001",
                    nickname="旧样本成员",
                    status="active",
                )
            )
            db.commit()
            summary = build_wechat_ipad_group_summary(db, channel, now=reference_time)

        self.assertEqual(summary["plugin_scope_snapshot"]["stage_key"], "ready_for_modeling")
        self.assertEqual(summary["coverage_snapshot"]["sync_freshness"]["label"], "过旧")
        self.assertEqual(summary["plugin_scope_brief"]["modeling_tier_label"], "可进入建模但样本偏旧")
        self.assertEqual(summary["plugin_scope_brief"]["modeling_tier_tone"], "outline")
        self.assertIn("应先刷新样本再收口规则", summary["plugin_scope_brief"]["modeling_tier_summary"])
        self.assertEqual(summary["plugin_scope_brief"]["primary_gap_label"], "群级启停模型")
        self.assertEqual(summary["plugin_scope_brief"]["blocker_family_label"], "模型前置")
        self.assertIn("否则无法落最小开关治理", summary["plugin_scope_brief"]["primary_gap_summary"])
        self.assertIn("可进入建模但样本偏旧；主 blocker：群级启停模型；下一步：可进入群级权限建模", summary["plugin_scope_brief"]["decision_summary"])

    def test_wechat_ipad_group_plugin_blocker_priority_covers_model_categories(self):
        enable_disable = wechat_ipad_service._build_chatroom_plugin_scope_blocker(
            stage_key="ready_for_modeling",
            missing_capabilities=[
                "缺群级 enable/disable 映射",
                "缺 allow/deny 持久化口径",
                "缺群级命令/能力边界模型",
                "缺审计与运营动作留痕",
            ],
        )
        allow_deny = wechat_ipad_service._build_chatroom_plugin_scope_blocker(
            stage_key="ready_for_modeling",
            missing_capabilities=[
                "缺 allow/deny 持久化口径",
                "缺群级命令/能力边界模型",
                "缺审计与运营动作留痕",
            ],
        )
        command_scope = wechat_ipad_service._build_chatroom_plugin_scope_blocker(
            stage_key="ready_for_modeling",
            missing_capabilities=[
                "缺群级命令/能力边界模型",
                "缺审计与运营动作留痕",
            ],
        )
        audit = wechat_ipad_service._build_chatroom_plugin_scope_blocker(
            stage_key="ready_for_modeling",
            missing_capabilities=["缺审计与运营动作留痕"],
        )

        self.assertEqual(enable_disable["family_label"], "模型前置")
        self.assertEqual(enable_disable["gap_label"], "群级启停模型")
        self.assertEqual(allow_deny["gap_label"], "allow/deny 边界")
        self.assertEqual(command_scope["gap_label"], "命令/能力边界")
        self.assertEqual(audit["gap_label"], "审计留痕")

    def test_wechat_ipad_overview_group_governance_snapshot_reuses_group_summary_decision(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            group_summary = build_wechat_ipad_group_summary(db, channel)
            overview = build_wechat_ipad_overview(channel, group_summary=group_summary)

        self.assertEqual(
            overview["plugin_scope_overview"]["headline"],
            group_summary["plugin_scope_overview"]["headline"],
        )
        self.assertEqual(
            overview["plugin_scope_overview"]["summary"],
            group_summary["plugin_scope_overview"]["summary"],
        )
        self.assertEqual(
            overview["plugin_scope_overview"]["primary_gap_label"],
            group_summary["plugin_scope_overview"]["primary_gap_label"],
        )
        self.assertEqual(
            overview["plugin_scope_overview"]["next_step_summary"],
            group_summary["plugin_scope_overview"]["next_step_summary"],
        )
        self.assertEqual(
            group_summary["group_plugin_governance_snapshot"]["decision_summary"],
            group_summary["plugin_scope_brief"]["decision_summary"],
        )
        self.assertEqual(
            group_summary["group_plugin_governance_snapshot"]["next_step_summary"],
            group_summary["plugin_scope_brief"]["next_step_summary"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["headline"],
            group_summary["group_plugin_governance_snapshot"]["headline"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["summary"],
            group_summary["group_plugin_governance_snapshot"]["summary"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["decision_summary"],
            group_summary["group_plugin_governance_snapshot"]["decision_summary"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["primary_gap_label"],
            group_summary["group_plugin_governance_snapshot"]["primary_gap_label"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["next_step_summary"],
            group_summary["group_plugin_governance_snapshot"]["next_step_summary"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["priority_label"],
            group_summary["group_plugin_governance_snapshot"]["priority_label"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["ops_digest"],
            group_summary["group_plugin_governance_snapshot"]["ops_digest"],
        )
        self.assertEqual(
            overview["plugin_scope_overview"]["single_overview_summary"],
            group_summary["plugin_scope_overview"]["single_overview_summary"],
        )
        self.assertEqual(
            overview["group_plugin_governance_snapshot"]["single_overview_summary"],
            group_summary["group_plugin_governance_snapshot"]["single_overview_summary"],
        )
        self.assertEqual(
            overview["group_enablement_model_snapshot"]["status_label"],
            group_summary["group_enablement_model_snapshot"]["status_label"],
        )
        self.assertEqual(
            overview["group_enablement_model_snapshot"]["recommended_primary_key"],
            group_summary["group_enablement_model_snapshot"]["recommended_primary_key"],
        )
        self.assertEqual(
            overview["group_allow_deny_model_snapshot"]["status_label"],
            group_summary["group_allow_deny_model_snapshot"]["status_label"],
        )
        self.assertEqual(
            overview["group_allow_deny_model_snapshot"]["recommended_dimension_keys"],
            group_summary["group_allow_deny_model_snapshot"]["recommended_dimension_keys"],
        )
        self.assertIn(
            "当前档位：尚未具备；建模判断：仍不可建模；优先级：高；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本",
            overview["group_plugin_governance_snapshot"]["overview_summary"],
        )

    def test_wechat_ipad_overview_group_governance_snapshot_builds_single_summary_when_modelable(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-overview-ready@chatroom",
                name="总览就绪群",
                status="active",
                member_count=6,
                last_synced_at=datetime.fromisoformat("2026-03-24T11:50:00+00:00"),
                extra_json=json.dumps({"source": "overview-ready-test"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add(
                ChatroomMember(
                    chatroom_id=chatroom.id,
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    member_external_id="wxid_member_overview_ready_001",
                    nickname="总览成员",
                    status="active",
                    last_synced_at=datetime.fromisoformat("2026-03-24T11:51:00+00:00"),
                )
            )
            db.commit()
            group_summary = build_wechat_ipad_group_summary(db, channel, now=datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc))
            overview = build_wechat_ipad_overview(channel, group_summary=group_summary)

        self.assertIn(
            "当前档位：可进入群级权限建模；建模判断：已具备建模前提；优先级：中；主 blocker：群级启停模型；下一步：可进入群级权限建模",
            overview["group_plugin_governance_snapshot"]["overview_summary"],
        )

    def test_wechat_ipad_channel_detail_renders_group_governance_single_summary_for_not_ready_and_ready(self):
        request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})

        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.asc())).all()

            not_ready_group_summary = build_wechat_ipad_group_summary(db, channel)
            not_ready_overview = build_wechat_ipad_overview(channel, group_summary=not_ready_group_summary)
            not_ready_single_summary = not_ready_overview["group_plugin_governance_snapshot"]["overview_summary"]
            not_ready_html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=not_ready_overview,
                wechat_ipad_group_summary=not_ready_group_summary,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-render-ready@chatroom",
                name="页面就绪群",
                status="active",
                member_count=5,
                last_synced_at=datetime.fromisoformat("2026-03-24T11:55:00+00:00"),
                extra_json=json.dumps({"source": "render-ready-test"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add(
                ChatroomMember(
                    chatroom_id=chatroom.id,
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    member_external_id="wxid_member_render_ready_001",
                    nickname="页面成员",
                    status="active",
                    last_synced_at=datetime.fromisoformat("2026-03-24T11:56:00+00:00"),
                )
            )
            db.commit()
            ready_group_summary = build_wechat_ipad_group_summary(db, channel, now=datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc))
            ready_overview = build_wechat_ipad_overview(channel, group_summary=ready_group_summary)
            ready_single_summary = ready_overview["group_plugin_governance_snapshot"]["overview_summary"]
            ready_html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=ready_overview,
                wechat_ipad_group_summary=ready_group_summary,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("群级治理单一总览：", not_ready_html)
        self.assertIn(not_ready_single_summary, not_ready_html)
        self.assertIn("群级插件权限单一总览", not_ready_html)
        self.assertIn("单一总览摘要：当前档位：尚未具备；建模判断：仍不可建模；优先级：高；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本，再回来看群级权限建模。", not_ready_html)
        self.assertIn("当前档位 尚未具备", not_ready_html)
        self.assertIn("优先级 高", not_ready_html)
        self.assertIn("主 blocker 群资料样本", not_ready_html)
        self.assertIn("边界提示：当前仅为群级插件权限治理的准备度/扩展位，不代表真实群级启停、allow/deny 或命令边界已经上线。", not_ready_html)
        self.assertIn("当前仅为群级插件权限治理的准备度/扩展位，不代表真实群级启停、allow/deny 或命令边界已经上线。", not_ready_html)
        self.assertIn("群级权限下一步</th><td><span class=\"chip chip-muted\">暂不可建模</span> 下一步先补首批群消息或群资料样本，再回来看群级权限建模。</td></tr>", not_ready_html)
        self.assertIn("下一步</th><td>下一步先补首批群消息或群资料样本，再回来看群级权限建模。</td></tr>", not_ready_html)
        self.assertIn("群级治理单一总览：", ready_html)
        self.assertIn(ready_single_summary, ready_html)
        self.assertIn("群级插件权限单一总览", ready_html)
        self.assertIn("单一总览摘要：当前档位：可进入群级权限建模；建模判断：已具备建模前提；优先级：中；主 blocker：群级启停模型；下一步：可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", ready_html)
        self.assertIn("当前档位 可进入群级权限建模", ready_html)
        self.assertIn("优先级 中", ready_html)
        self.assertIn("主 blocker 群级启停模型", ready_html)
        self.assertIn("边界提示：当前仅为群级插件权限治理的准备度/扩展位，不代表真实群级启停、allow/deny 或命令边界已经上线。", ready_html)
        self.assertIn("建模判断：已具备建模前提", ready_single_summary)
        self.assertIn("优先级：中", ready_single_summary)
        self.assertIn("主 blocker：群级启停模型", ready_single_summary)
        self.assertIn("群级治理单一总览摘要：当前档位：可进入群级权限建模；建模判断：已具备建模前提；优先级：中；主 blocker：群级启停模型；下一步：可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", ready_html)
        self.assertIn("群级权限下一步</th><td><span class=\"chip chip-success\">可进入群级权限建模</span> 可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。</td></tr>", ready_html)
        self.assertIn("下一步</th><td>可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。</td></tr>", ready_html)

    def test_wechat_ipad_group_summary_builds_sync_freshness_buckets(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        cases = [
            ("新鲜", "2026-03-24T11:55:00+00:00", "fresh", "新鲜"),
            ("偏旧", "2026-03-24T11:20:00+00:00", "stale", "偏旧"),
            ("过旧", "2026-03-24T10:30:00+00:00", "expired", "过旧"),
            ("尚无时间样本", None, "no_sample", "尚无时间样本"),
        ]

        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            for name, synced_at, expected_key, expected_label in cases:
                with self.subTest(name):
                    db.query(ChatroomMember).delete()
                    db.query(Chatroom).delete()
                    if synced_at is not None:
                        db.add(
                            Chatroom(
                                bot_id=channel.bot_id,
                                channel_id=channel.id,
                                external_id=f"room-{expected_key}@chatroom",
                                name=f"样本-{expected_label}",
                                status="active",
                                member_count=3,
                                last_synced_at=datetime.fromisoformat(synced_at),
                                extra_json=json.dumps({"source": "freshness-test"}, ensure_ascii=False),
                            )
                        )
                    db.commit()
                    summary = build_wechat_ipad_group_summary(db, channel, now=reference_time)

                    self.assertEqual(summary["coverage_snapshot"]["sync_freshness"]["key"], expected_key)
                    self.assertEqual(summary["coverage_snapshot"]["sync_freshness"]["label"], expected_label)
                    self.assertEqual(summary["ops_snapshot"]["freshness_key"], expected_key)
                    self.assertEqual(summary["ops_snapshot"]["freshness_label"], expected_label)
                    self.assertEqual(summary["stage_snapshot"]["freshness_label"], expected_label)
                    self.assertIn(f"最近同步时效：{expected_label}", summary["stage_snapshot"]["operator_summary"])

    def test_wechat_ipad_channel_detail_shows_chatroom_model_empty_state(self):
        self.login("ops", "ops123")
        response = self.client.get("/channels/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("微信群资料本地骨架", response.text)
        self.assertIn("本地群数", response.text)
        self.assertIn("当前群资料同步判断：尚未形成样本", response.text)
        self.assertIn("关键证据：当前没有本地 chatroom 或 chatroom_members 样本。", response.text)
        self.assertIn("下一步：先等待首批群消息或群资料同步落库，再判断是否进入下一档。", response.text)
        self.assertIn("当前群资料同步档位：尚未形成样本", response.text)
        self.assertIn("群级插件权限准备度", response.text)
        self.assertIn("群级插件权限准备度：尚未具备", response.text)
        self.assertIn("当前仅为群级插件权限治理的准备度/扩展位", response.text)
        self.assertIn("主 blocker 前置短句", response.text)
        self.assertIn("样本前置 / 群资料样本：群资料样本不足；先补群资料样本，补完后才有资格进入下一步。", response.text)
        self.assertIn("仍缺模型摘要", response.text)
        self.assertIn("群资料样本", response.text)
        self.assertIn("当前最影响下一步的是群资料样本不足", response.text)
        self.assertIn("群级权限一眼判断", response.text)
        self.assertIn("仍不可建模", response.text)
        self.assertIn("仍不可建模；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本", response.text)
        self.assertIn("群资料同步覆盖度", response.text)
        self.assertIn("最近同步摘要", response.text)
        self.assertIn("暂无群资料样本", response.text)
        self.assertIn("最近同步时效", response.text)
        self.assertIn("尚无时间样本", response.text)
        self.assertIn("同步一眼判断", response.text)
        self.assertIn("尚未形成样本；最近同步时效：尚无时间样本；当前没有本地 chatroom 或 chatroom_members 样本", response.text)
        self.assertIn("群资料覆盖 0 / 0 (0%)", response.text)
        self.assertIn("尚未形成样本", response.text)
        self.assertIn("尚未形成群资料同步样本", response.text)
        self.assertIn("尚未观测到群资料", response.text)
        self.assertIn("最近观测群资料", response.text)

    def test_wechat_ipad_channel_detail_shows_recent_chatroom_visibility_summary(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-visible-001@chatroom",
                name="可见性联调群",
                status="message_observed",
                member_count=1,
                extra_json=json.dumps({"source": "wechat_ipad_message_ingest"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add(
                ChatroomMember(
                    chatroom_id=chatroom.id,
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    member_external_id="wxid_member_visible_001",
                    nickname="测试成员",
                    status="message_observed",
                )
            )
            db.commit()

        self.login("ops", "ops123")
        response = self.client.get("/channels/1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("可见性联调群", response.text)
        self.assertIn("wechat_ipad_message_ingest", response.text)
        self.assertIn("当前群资料同步档位：仅消息骨架", response.text)
        self.assertIn("群资料同步覆盖度", response.text)
        self.assertIn("最近同步摘要", response.text)
        self.assertIn("消息骨架占比 1 / 1 (100%)", response.text)
        self.assertIn("群资料覆盖 0 / 1 (0%)", response.text)
        self.assertIn("群级插件权限准备度：仅有群资料骨架", response.text)
        self.assertIn("当前仍处于“仅有群资料骨架”", response.text)
        self.assertIn("当前最影响下一步的是群资料样本不足", response.text)
        self.assertIn("样本前置 / 群资料样本：群资料样本不足；先补群资料样本，补完后才有资格进入下一步。", response.text)
        self.assertIn("群级权限样本门槛：未达下一步", response.text)
        self.assertIn("样本门槛未满足", response.text)
        self.assertIn("群级权限一眼判断", response.text)
        self.assertIn("仍不可建模", response.text)
        self.assertIn("仍不可建模；主 blocker：群资料样本；下一步：下一步先补真实群资料同步", response.text)
        self.assertIn("仅消息骨架", response.text)
        self.assertIn("全部停留在 message_observed 骨架阶段", response.text)
        self.assertIn("当前仍有 1 个群停留在 message_observed 骨架阶段", response.text)
        self.assertIn("消息观测骨架", response.text)

    def test_wechat_ipad_chatroom_list_page_renders_groups_and_scope_summary(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            plugin = Plugin(
                code="weather_group_list_scope",
                display_name="Weather Group List Plugin",
                plugin_type="command",
                default_enabled=True,
            )
            db.add(plugin)
            db.flush()
            binding = BotPlugin(
                bot_id=channel.bot_id,
                plugin_id=plugin.id,
                status="enabled",
                runtime_status="idle",
            )
            db.add(binding)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-list-001@chatroom",
                name="group-list-qa",
                owner_external_id="wxid_owner_list_001",
                status="members_synced",
                member_count=3,
                extra_json=json.dumps({"source": "wechat_ipad_chatroom_sync"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add_all(
                [
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_owner_list_001",
                        nickname="owner-user",
                        display_name="owner-display",
                        role="owner",
                        status="active",
                    ),
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_admin_list_002",
                        nickname="admin-user",
                        display_name="admin-display",
                        role="admin",
                        status="active",
                    ),
                ]
            )
            db.flush()
            db.add(
                GroupPluginScope(
                    channel_id=channel.id,
                    chatroom_id=chatroom.id,
                    bot_plugin_id=binding.id,
                    scope_status="overridden",
                    enabled=False,
                    action_scope="all",
                    policy_effect="group_override",
                )
            )
            db.commit()

        self.login("ops", "ops123")
        response = self.client.get("/channels/1/chatrooms")
        self.assertEqual(response.status_code, 200)
        self.assertIn("群列表与治理总览", response.text)
        self.assertIn("group-list-qa", response.text)
        self.assertIn("owner-display", response.text)
        self.assertIn("总绑定 1 / 覆盖 1", response.text)
        self.assertIn("有效启用 0 / 停用 1", response.text)
        self.assertIn("查看成员与权限", response.text)
        self.assertIn("/channels/1/chatrooms/", response.text)

    def test_wechat_ipad_chatroom_detail_page_renders_members_and_bot_plugin_scope(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            plugin = Plugin(
                code="weather_detail_scope",
                display_name="Weather Plugin",
                plugin_type="command",
                default_enabled=True,
            )
            db.add(plugin)
            db.flush()
            binding = BotPlugin(
                bot_id=channel.bot_id,
                plugin_id=plugin.id,
                status="enabled",
                runtime_status="idle",
                last_execution_status="success",
                last_execution_summary="last run ok",
            )
            db.add(binding)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-detail-001@chatroom",
                name="chatroom-detail-qa",
                owner_external_id="wxid_owner_001",
                status="active",
                member_count=2,
                extra_json=json.dumps({"source": "wechat_ipad_group_sync"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add_all(
                [
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_owner_001",
                        nickname="owner-user",
                        display_name="owner-display",
                        role="owner",
                        status="active",
                    ),
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_member_002",
                        nickname="member-user",
                        display_name="member-display",
                        role="member",
                        status="active",
                    ),
                ]
            )
            db.commit()
            chatroom_id = chatroom.id

        self.login("ops", "ops123")
        response = self.client.get(f"/channels/1/chatrooms/{chatroom_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("chatroom-detail-qa", response.text)
        self.assertIn("owner-display", response.text)
        self.assertIn("wxid_owner_001", response.text)
        self.assertIn("weather_detail_scope", response.text)
        self.assertIn("member-display", response.text)
        self.assertIn("/scopes/", response.text)
        self.assertIn("runtime=idle", response.text)
        self.assertIn("bot_plugin", response.text)
        self.assertIn("room-detail-001@chatroom", response.text)
        self.assertIn("/channels/1/chatrooms/", response.text)
        self.assertIn("/disable", response.text)
        with SessionLocal() as db:
            scopes = db.scalars(select(GroupPluginScope).where(GroupPluginScope.chatroom_id == chatroom_id)).all()
            self.assertEqual(len(scopes), 1)
            self.assertEqual(scopes[0].scope_status, "inherited")

    def test_workwechat_chatroom_detail_and_scope_mode_buttons_do_not_error(self):
        with SessionLocal() as db:
            bot = Bot(name="企微测试 Bot")
            db.add(bot)
            db.flush()
            channel = Channel(
                bot_id=bot.id,
                name="企微 Hook 测试",
                channel_type="workwechat",
                api_base="http://127.0.0.1:8181",
                adapter_config_json=json.dumps({"headers": {"X-Provider-Token": "provider-token"}}, ensure_ascii=False),
            )
            plugin = Plugin(
                code="workwechat_scope_detail",
                display_name="企微群权限插件",
                plugin_type="command",
                default_enabled=True,
            )
            db.add_all([channel, plugin])
            db.flush()
            binding = BotPlugin(
                bot_id=bot.id,
                plugin_id=plugin.id,
                status="enabled",
                runtime_status="idle",
            )
            chatroom = Chatroom(
                bot_id=bot.id,
                channel_id=channel.id,
                external_id="R:workwechat-room-detail",
                name="企微成员权限群",
                status="message_observed",
                member_count=1,
                extra_json=json.dumps({"source": "workwechat_message_ingest"}, ensure_ascii=False),
            )
            conversation = Conversation(
                bot_id=bot.id,
                channel_id=channel.id,
                channel_type="workwechat",
                external_id="R:workwechat-room-detail",
                title="企微成员权限群",
                chat_type="group",
            )
            db.add_all([binding, chatroom, conversation])
            db.flush()
            db.add_all(
                [
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=bot.id,
                        channel_id=channel.id,
                        member_external_id="ww-member-001",
                        nickname="企微成员",
                        display_name="群内企微成员",
                        role="member",
                        status="active",
                    ),
                    Message(
                        conversation_id=conversation.id,
                        direction="in",
                        sender_id="ww-member-001",
                        sender_name="企微成员",
                        message_type="text",
                        content="hello workwechat detail",
                        status="received",
                    ),
                ]
            )
            db.commit()
            channel_id = channel.id
            chatroom_id = chatroom.id
            binding_id = binding.id

        self.login("ops", "ops123")
        response = self.client.get(f"/channels/{channel_id}/chatrooms/{chatroom_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("企微成员权限群", response.text)
        self.assertIn("hello workwechat detail", response.text)
        self.assertIn("群内企微成员", response.text)
        self.assertIn("workwechat_scope_detail", response.text)
        self.assertIn(f"/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{binding_id}/mode", response.text)
        self.assertNotIn(f"/channels/{channel_id}/chatrooms/{chatroom_id}/media-download", response.text)

        mode_response = self.client.post(
            f"/channels/{channel_id}/chatrooms/{chatroom_id}/scopes/{binding_id}/mode",
            data={"mode": "disabled"},
            follow_redirects=False,
        )
        self.assertEqual(mode_response.status_code, 303)

        with SessionLocal() as db:
            scope = db.scalar(
                select(GroupPluginScope).where(
                    GroupPluginScope.channel_id == channel_id,
                    GroupPluginScope.chatroom_id == chatroom_id,
                    GroupPluginScope.bot_plugin_id == binding_id,
                )
            )
            self.assertIsNotNone(scope)
            self.assertEqual(scope.scope_status, "overridden")
            self.assertFalse(scope.enabled)

    def test_wechat_ipad_chatroom_scope_enable_disable_and_reset_actions(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            plugin = Plugin(
                code="weather_scope_toggle",
                display_name="Weather Scope Plugin",
                plugin_type="command",
                default_enabled=True,
            )
            db.add(plugin)
            db.flush()
            binding = BotPlugin(
                bot_id=channel.bot_id,
                plugin_id=plugin.id,
                status="enabled",
                runtime_status="idle",
            )
            db.add(binding)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-scope-toggle@chatroom",
                name="scope-toggle-room",
                status="members_synced",
                member_count=1,
            )
            db.add(chatroom)
            db.commit()
            binding_id = binding.id
            chatroom_id = chatroom.id

        self.login("ops", "ops123")
        disable_response = self.client.post(
            f"/channels/1/chatrooms/{chatroom_id}/scopes/{binding_id}/disable",
            follow_redirects=False,
        )
        self.assertEqual(disable_response.status_code, 303)

        with SessionLocal() as db:
            scope = db.scalar(
                select(GroupPluginScope).where(
                    GroupPluginScope.chatroom_id == chatroom_id,
                    GroupPluginScope.bot_plugin_id == binding_id,
                )
            )
            self.assertIsNotNone(scope)
            self.assertEqual(scope.scope_status, "overridden")
            self.assertFalse(scope.enabled)

        enable_response = self.client.post(
            f"/channels/1/chatrooms/{chatroom_id}/scopes/{binding_id}/enable",
            follow_redirects=False,
        )
        self.assertEqual(enable_response.status_code, 303)

        with SessionLocal() as db:
            scope = db.scalar(
                select(GroupPluginScope).where(
                    GroupPluginScope.chatroom_id == chatroom_id,
                    GroupPluginScope.bot_plugin_id == binding_id,
                )
            )
            self.assertIsNotNone(scope)
            self.assertEqual(scope.scope_status, "overridden")
            self.assertTrue(scope.enabled)

        reset_response = self.client.post(
            f"/channels/1/chatrooms/{chatroom_id}/scopes/{binding_id}/reset",
            follow_redirects=False,
        )
        self.assertEqual(reset_response.status_code, 303)

        with SessionLocal() as db:
            scope = db.scalar(
                select(GroupPluginScope).where(
                    GroupPluginScope.chatroom_id == chatroom_id,
                    GroupPluginScope.bot_plugin_id == binding_id,
                )
            )
            self.assertIsNotNone(scope)
            self.assertEqual(scope.scope_status, "inherited")
            self.assertTrue(scope.enabled)
    def test_wechat_ipad_chatroom_profile_sync_promotes_status_from_message_observed(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-promote-001@chatroom",
                name="初始骨架群",
                status="message_observed",
            )
            db.add(chatroom)
            db.commit()

            upsert_wechat_ipad_chatroom_profile(
                db,
                channel,
                chatroom_external_id="room-promote-001@chatroom",
                profile_payload={
                    "Data": {
                        "ContactList": [
                            {
                                "NickName": "已补资料群",
                                "Remark": "群备注",
                                "ChatRoomOwner": "wxid_owner_promote",
                                "MemberCount": 2,
                            }
                        ]
                    }
                },
                member_payload={
                    "Data": {
                        "NewChatroomData": {
                            "ChatRoomMember": [
                                {"UserName": "wxid_owner_promote", "NickName": "群主", "DisplayName": "群主", "MemberFlag": 1},
                                {"UserName": "wxid_member_promote", "NickName": "成员", "DisplayName": "成员显示名", "MemberFlag": 0},
                            ]
                        }
                    }
                },
            )
            db.commit()
            db.refresh(chatroom)

            self.assertEqual(chatroom.status, "members_synced")
            self.assertEqual(chatroom.name, "已补资料群")
            self.assertEqual(chatroom.owner_external_id, "wxid_owner_promote")
            self.assertEqual(chatroom.member_count, 2)
            members = db.scalars(select(ChatroomMember).where(ChatroomMember.chatroom_id == chatroom.id)).all()
            self.assertEqual(len(members), 2)

    def test_wechat_ipad_chatroom_member_status_marks_left_and_recovers_when_back(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-member-status-001@chatroom",
                name="成员状态群",
                status="members_synced",
                member_count=2,
            )
            db.add(chatroom)
            db.flush()
            db.add_all(
                [
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_member_keep",
                        nickname="常驻成员",
                        display_name="常驻成员",
                        status="active",
                    ),
                    ChatroomMember(
                        chatroom_id=chatroom.id,
                        bot_id=channel.bot_id,
                        channel_id=channel.id,
                        member_external_id="wxid_member_return",
                        nickname="回归成员",
                        display_name="回归成员",
                        status="active",
                    ),
                ]
            )
            db.commit()

            upsert_wechat_ipad_chatroom_profile(
                db,
                channel,
                chatroom_external_id="room-member-status-001@chatroom",
                member_payload={
                    "Data": {
                        "NewChatroomData": {
                            "ChatRoomMember": [
                                {"UserName": "wxid_member_keep", "NickName": "常驻成员", "DisplayName": "常驻成员", "MemberFlag": 0},
                            ]
                        }
                    }
                },
                synced_at=datetime(2026, 3, 25, 8, 0, 0, tzinfo=timezone.utc),
            )
            db.commit()

            left_member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.chatroom_id == chatroom.id,
                    ChatroomMember.member_external_id == "wxid_member_return",
                )
            )
            self.assertIsNotNone(left_member)
            self.assertEqual(left_member.status, "left")

            upsert_wechat_ipad_chatroom_profile(
                db,
                channel,
                chatroom_external_id="room-member-status-001@chatroom",
                member_payload={
                    "Data": {
                        "NewChatroomData": {
                            "ChatRoomMember": [
                                {"UserName": "wxid_member_keep", "NickName": "常驻成员", "DisplayName": "常驻成员", "MemberFlag": 0},
                                {"UserName": "wxid_member_return", "NickName": "回归成员", "DisplayName": "回归成员", "MemberFlag": 0},
                            ]
                        }
                    }
                },
                synced_at=datetime(2026, 3, 25, 9, 0, 0, tzinfo=timezone.utc),
            )
            db.commit()

            returned_member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.chatroom_id == chatroom.id,
                    ChatroomMember.member_external_id == "wxid_member_return",
                )
            )
            self.assertIsNotNone(returned_member)
            self.assertEqual(returned_member.status, "active")

    def test_wechat_ipad_channel_detail_shows_group_sync_freshness_chip(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
            db.add(
                Chatroom(
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    external_id="room-freshness-chip@chatroom",
                    name="时效摘要群",
                    status="active",
                    member_count=5,
                    last_synced_at=datetime.fromisoformat("2026-03-24T10:30:00+00:00"),
                    extra_json=json.dumps({"source": "freshness-page"}, ensure_ascii=False),
                )
            )
            db.commit()
            db.refresh(channel)
            group_summary = build_wechat_ipad_group_summary(db, channel, now=reference_time)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=build_wechat_ipad_overview(channel, now=reference_time),
                wechat_ipad_group_summary=group_summary,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("最近同步时效", html)
        self.assertIn('最近同步时效</th><td><span class="chip chip-outline">过旧</span> 最近群资料同步已过旧', html)
        self.assertIn("同步一眼判断</th><td><span class=\"chip chip-outline\">已有群资料，无成员快照</span> 已有群资料，无成员快照；最近同步时效：过旧；", html)
        self.assertIn("群资料时效：", html)
        self.assertIn("群资料最近同步时效：最近群资料同步已过旧", html)
        self.assertIn("群级插件权限准备度", html)
        self.assertIn("群级权限治理摘要：已具备建模前提", html)
        self.assertIn("群成员样本", html)
        self.assertIn("当前最影响下一步的是群成员样本缺失", html)
        self.assertIn("群级权限样本门槛：可进入下一步", html)
        self.assertIn("样本门槛已满足", html)
        self.assertIn("<span class=\"chip chip-outline\">过旧</span>", html)
        self.assertIn("样本门槛下一步：可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", html)
        self.assertIn("当前仅为群级插件权限治理的准备度/扩展位", html)

    def test_wechat_ipad_channel_detail_renders_plugin_scope_card_for_empty_state(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
            group_summary = build_wechat_ipad_group_summary(db, channel)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=build_wechat_ipad_overview(channel, group_summary=group_summary),
                wechat_ipad_group_summary=group_summary,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("群级插件权限准备度", html)
        self.assertIn("尚未具备", html)
        self.assertIn("先补首批群消息或群资料样本", html)
        self.assertIn("主 blocker 前置短句", html)
        self.assertIn("样本前置 / 群资料样本：群资料样本不足；先补群资料样本，补完后才有资格进入下一步。", html)
        self.assertIn("群级权限治理摘要：仍不可建模", html)
        self.assertIn("前置条件：群资料=0，成员记录群=0，bot 插件绑定启用/总计=3/4，插件镜像=4。", html)
        self.assertIn("群级治理摘要</th><td><strong>群级权限治理摘要：仍不可建模</strong>", html)
        self.assertIn("群级权限样本门槛</th><td><span class=\"chip chip-muted\">样本门槛未满足</span>", html)
        self.assertIn("样本门槛下一步</th><td>下一步先补首批群消息或群资料样本，再回来看群级权限建模。</td></tr>", html)
        self.assertIn("群级治理总览</th><td>当前档位：尚未具备；建模判断：仍不可建模；优先级：高；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本，再回来看群级权限建模。</td></tr>", html)
        self.assertIn("群级治理边界</th><td>当前仅为群级插件权限治理的准备度/扩展位，不代表真实群级启停、allow/deny 或命令边界已经上线。</td></tr>", html)
        self.assertIn("群级启停模型口径</th><td><span class=\"chip chip-outline\">已定义草案级口径</span>", html)
        self.assertIn("群级 allow/deny 口径</th><td><span class=\"chip chip-outline\">已定义草案级口径</span>", html)
        self.assertIn("当前档位：", html)
        self.assertIn("主 blocker：", html)
        self.assertIn("群级治理缺口", html)
        self.assertIn("群级治理运维摘要：当前阶段=尚未具备；关键证据=前置条件：群资料=0，成员记录群=0，bot 插件绑定启用/总计=3/4，插件镜像=4；下一步=下一步先补首批群消息或群资料样本，再回来看群级权限建模。", html)
        self.assertIn("仍缺模型摘要", html)
        self.assertIn("群资料样本", html)
        self.assertIn("当前最影响下一步的是群资料样本不足", html)
        self.assertIn("群级权限样本门槛：未达下一步", html)
        self.assertIn("样本门槛未满足", html)
        self.assertIn("样本门槛证据：前置条件：群资料=0，成员记录群=0，bot 插件绑定启用/总计=3/4，插件镜像=4；样本时效=尚无时间样本。", html)
        self.assertIn("样本门槛下一步：下一步先补首批群消息或群资料样本，再回来看群级权限建模。", html)
        self.assertIn("样本门槛边界：当前仅为群级插件权限治理的准备度/扩展位，不代表真实群级启停、allow/deny 或命令边界已经上线。", html)
        self.assertIn("群级治理单一总览摘要：当前档位：尚未具备；建模判断：仍不可建模；优先级：高；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本，再回来看群级权限建模。", html)
        decision_summary = "仍不可建模；主 blocker：群资料样本；下一步：下一步先补首批群消息或群资料样本"
        self.assertIn(decision_summary, html)
        self.assertGreaterEqual(html.count(decision_summary), 2)
        self.assertIn("群资料实体 0", html)
        self.assertIn("成员记录群 0", html)
        self.assertIn("bot 插件绑定 3 / 4", html)
        self.assertIn("结构化摘要：建模判断=仍不可建模；准备度=尚未具备；优先级=高；主 blocker=群资料样本；下一步=下一步先补首批群消息或群资料样本，再回来看群级权限建模。", html)
        self.assertIn("运维摘要：当前阶段=尚未具备；关键证据=前置条件：群资料=0，成员记录群=0，bot 插件绑定启用/总计=3/4，插件镜像=4；下一步=下一步先补首批群消息或群资料样本，再回来看群级权限建模。", html)
        self.assertIn("群级权限运维摘要</th><td>当前阶段=尚未具备；关键证据=前置条件：群资料=0，成员记录群=0，bot 插件绑定启用/总计=3/4，插件镜像=4；下一步=下一步先补首批群消息或群资料样本，再回来看群级权限建模。", html)
        self.assertIn("仍缺模型：缺群级 enable/disable 映射 / 缺 allow/deny 持久化口径 / 缺群级命令/能力边界模型 / 缺审计与运营动作留痕", html)
        self.assertIn("当前仅为群级插件权限治理的准备度/扩展位", html)
        self.assertIn("群级启停模型口径", html)
        self.assertIn("建议最小主键：channel_id + chatroom_id(或 chatroom_external_id) + bot_plugin_id", html)
        self.assertIn("口径维度：<code>channel_id</code> / <code>chatroom_id_or_external_id</code> / <code>bot_plugin_id</code> / <code>plugin_projection</code>", html)
        self.assertIn("当前仅定义群级启停模型口径摘要与扩展位，不代表真实群级启停控制、写入接口或权限系统已经上线。", html)
        self.assertIn("群级 allow/deny 口径", html)
        self.assertIn("建议判定维度：<code>channel_id</code> / <code>chatroom_id_or_external_id</code> / <code>bot_plugin_id</code> / <code>action_scope</code> / <code>policy_effect</code>", html)
        self.assertIn("当前已把群级 allow/deny 收口到只读草案摘要", html)
        self.assertIn("当前仅定义群级 allow/deny 口径摘要与扩展位，不代表真实群级 allow/deny 控制、写入接口、判定执行或权限系统已经上线。", html)
        self.assertIn("仍不可建模", html)

    def test_wechat_ipad_channel_detail_renders_plugin_scope_card_for_modelable_state(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
            chatroom = Chatroom(
                bot_id=channel.bot_id,
                channel_id=channel.id,
                external_id="room-plugin-scope-ready@chatroom",
                name="插件治理建模群",
                status="active",
                member_count=12,
                last_synced_at=datetime.fromisoformat("2026-03-24T11:55:00+00:00"),
                extra_json=json.dumps({"source": "plugin-scope-ready-page"}, ensure_ascii=False),
            )
            db.add(chatroom)
            db.flush()
            db.add(
                ChatroomMember(
                    chatroom_id=chatroom.id,
                    bot_id=channel.bot_id,
                    channel_id=channel.id,
                    member_external_id="wxid_plugin_scope_ready_member",
                    nickname="建模成员",
                    status="active",
                )
            )
            db.commit()
            group_summary = build_wechat_ipad_group_summary(db, channel)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=build_wechat_ipad_overview(channel, group_summary=group_summary),
                wechat_ipad_group_summary=group_summary,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("群级权限治理摘要：已具备建模前提", html)
        self.assertIn("已具备建模前提；主 blocker：群级启停模型；下一步：可进入群级权限建模", html)
        self.assertIn("群级治理摘要</th><td><strong>群级权限治理摘要：已具备建模前提</strong>", html)
        self.assertIn("群级权限样本门槛</th><td><span class=\"chip chip-success\">样本门槛已满足</span>", html)
        self.assertIn("样本门槛下一步</th><td>可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。</td></tr>", html)
        self.assertIn("群级治理总览</th><td>当前档位：可进入群级权限建模；建模判断：已具备建模前提；优先级：中；主 blocker：群级启停模型；下一步：可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。</td></tr>", html)
        self.assertIn("群级治理运维摘要：当前阶段=可进入群级权限建模；关键证据=前置条件：群资料=1，成员记录群=1，bot 插件绑定启用/总计=3/4，插件镜像=4；下一步=可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", html)
        self.assertIn("群级启停模型口径</th><td><span class=\"chip chip-success\">已定义草案级口径</span>", html)
        self.assertIn("群级 allow/deny 口径</th><td><span class=\"chip chip-success\">已定义草案级口径</span>", html)
        self.assertIn("当前档位：", html)
        self.assertIn("主 blocker：", html)
        self.assertIn("群级启停模型", html)
        self.assertIn("群级权限样本门槛：可进入下一步", html)
        self.assertIn("样本门槛已满足", html)
        self.assertIn("样本门槛证据：前置条件：群资料=1，成员记录群=1，bot 插件绑定启用/总计=3/4，插件镜像=4；样本时效=新鲜。", html)
        self.assertIn("样本门槛下一步：可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", html)
        self.assertIn("准备度证据：前置条件：群资料=1，成员记录群=1，bot 插件绑定启用/总计=3/4，插件镜像=4。", html)
        self.assertIn("群级治理单一总览摘要：当前档位：可进入群级权限建模；建模判断：已具备建模前提；优先级：中；主 blocker：群级启停模型；下一步：可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", html)
        self.assertIn("结构化摘要：建模判断=已具备建模前提；准备度=可进入群级权限建模；优先级=中；主 blocker=群级启停模型；下一步=可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", html)
        self.assertIn("运维摘要：当前阶段=可进入群级权限建模；关键证据=前置条件：群资料=1，成员记录群=1，bot 插件绑定启用/总计=3/4，插件镜像=4；下一步=可进入群级权限建模；优先定义群级启停 / allow/deny / 能力边界口径。", html)
        self.assertIn("当前仅为群级插件权限治理的准备度/扩展位", html)
        self.assertIn("建议最小主键：channel_id + chatroom_id(或 chatroom_external_id) + bot_plugin_id", html)
        self.assertIn("关系口径：channel -&gt; chatroom -&gt; bot_plugin 为最小控制链路；plugin/plugin_code 仅作镜像展示与草案对账。", html)
        self.assertIn("当前仅定义群级启停模型口径摘要与扩展位，不代表真实群级启停控制、写入接口或权限系统已经上线。", html)
        self.assertIn("建议判定维度：<code>channel_id</code> / <code>chatroom_id_or_external_id</code> / <code>bot_plugin_id</code> / <code>action_scope</code> / <code>policy_effect</code>", html)
        self.assertIn("关系口径：channel -&gt; chatroom -&gt; bot_plugin 为主体链路，再补 action_scope + policy_effect 表达群级 allow/deny 草案边界。", html)
        self.assertIn("当前仅定义群级 allow/deny 口径摘要与扩展位，不代表真实群级 allow/deny 控制、写入接口、判定执行或权限系统已经上线。", html)

    def test_wechat_ipad_group_message_ingest_upserts_chatroom_and_member(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            result = ingest_inbound_event(
                db,
                channel,
                {
                    "Event": "AddMsg",
                    "Wxid": "wxid_demo_account_001",
                    "RoomName": "研发运维群",
                    "Data": {
                        "FromUserName": {"string": "room-autofill@chatroom"},
                        "ToUserName": {"string": "wxid_demo_account_001"},
                        "Content": {"string": "wxid_member_a:\n第一条群消息"},
                        "MsgType": 1,
                        "MsgId": "evt-chatroom-autofill-001",
                        "CreateTime": 1712345678,
                    },
                },
                source="wechat_ipad_sync",
            )
            self.assertTrue(result["mapped"])

            chatroom = db.scalar(select(Chatroom).where(Chatroom.external_id == "room-autofill@chatroom"))
            self.assertIsNotNone(chatroom)
            self.assertEqual(chatroom.channel_id, channel.id)
            self.assertEqual(chatroom.bot_id, channel.bot_id)
            self.assertEqual(chatroom.name, "研发运维群")
            self.assertEqual(chatroom.status, "message_observed")
            self.assertIn("wechat_ipad_message_ingest", chatroom.extra_json or "")

            member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.chatroom_id == chatroom.id,
                    ChatroomMember.member_external_id == "wxid_member_a",
                )
            )
            self.assertIsNotNone(member)
            self.assertEqual(member.nickname, "wxid_member_a")
            self.assertEqual(member.status, "message_observed")

    def test_wechat_ipad_group_message_ingest_reuses_existing_chatroom_member(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            first = {
                "Event": "AddMsg",
                "Wxid": "wxid_demo_account_001",
                "RoomName": "研发运维群",
                "Data": {
                    "FromUserName": {"string": "room-autofill-reuse@chatroom"},
                    "ToUserName": {"string": "wxid_demo_account_001"},
                    "Content": {"string": "wxid_member_reuse:\n第一次消息"},
                    "MsgType": 1,
                    "MsgId": "evt-chatroom-autofill-002",
                    "CreateTime": 1712345678,
                },
            }
            second = {
                "Event": "AddMsg",
                "Wxid": "wxid_demo_account_001",
                "RoomName": "研发运维群-已更新",
                "Data": {
                    "FromUserName": {"string": "room-autofill-reuse@chatroom"},
                    "ToUserName": {"string": "wxid_demo_account_001"},
                    "Content": {"string": "wxid_member_reuse:\n第二次消息"},
                    "MsgType": 1,
                    "MsgId": "evt-chatroom-autofill-003",
                    "CreateTime": 1712345778,
                },
            }

            ingest_inbound_event(db, channel, first, source="wechat_ipad_sync")
            first_chatroom = db.scalar(select(Chatroom).where(Chatroom.external_id == "room-autofill-reuse@chatroom"))
            first_member = db.scalar(
                select(ChatroomMember).where(
                    ChatroomMember.chatroom_id == first_chatroom.id,
                    ChatroomMember.member_external_id == "wxid_member_reuse",
                )
            )
            first_synced_at = first_member.last_synced_at

            ingest_inbound_event(db, channel, second, source="wechat_ipad_sync")
            db.expire_all()

            chatrooms = db.scalars(select(Chatroom).where(Chatroom.external_id == "room-autofill-reuse@chatroom")).all()
            members = db.scalars(
                select(ChatroomMember).where(ChatroomMember.member_external_id == "wxid_member_reuse")
            ).all()
            self.assertEqual(len(chatrooms), 1)
            self.assertEqual(len(members), 1)
            self.assertEqual(chatrooms[0].name, "研发运维群-已更新")
            self.assertGreater(members[0].last_synced_at, first_synced_at)

    def test_wechat_ipad_extract_sync_fields_prefers_add_msgs_and_real_sync_keys(self):
        payload = {
            "Ret": 0,
            "Data": {
                "List": [{"MsgId": "legacy-list"}],
                "AddMsgs": [{"MsgId": "real-add-msg"}],
                "Synckey": "sync-key-xyz",
                "ContinueFlag": 0,
            },
            "http_status": 200,
        }

        sync_fields = WechatIpadApiClient.extract_sync_fields(payload)

        self.assertEqual(sync_fields["list"], [{"MsgId": "real-add-msg"}])
        self.assertEqual(sync_fields["add_msgs"], [{"MsgId": "real-add-msg"}])
        self.assertEqual(sync_fields["sync_key"], "sync-key-xyz")
        self.assertEqual(sync_fields["continue_flag"], 0)

    def test_wechat_ipad_extract_qr_and_account_fields_from_abot_shapes(self):
        payload = {
            "Ret": 0,
            "Message": "登陆成功",
            "Data": {
                "expiredTime": 120,
                "QrCodeResponse": {
                    "Uuid": "uuid-awaken-001",
                    "QrUrl": "http://weixin.qq.com/x/uuid-awaken-001",
                    "expiredTime": 120,
                },
                "acctSectResp": {
                    "userName": "wxid_runtime_001",
                    "nickName": "运行号",
                    "alias": "abot_runtime",
                    "bindMobile": "13800001111",
                },
            },
            "http_status": 200,
        }

        qr_fields = WechatIpadApiClient.extract_qr_fields(payload)
        account_fields = WechatIpadApiClient.extract_account_fields(payload)

        self.assertEqual(qr_fields["uuid"], "uuid-awaken-001")
        self.assertEqual(qr_fields["qr_url"], "http://weixin.qq.com/x/uuid-awaken-001")
        self.assertNotIn("qr_base64", qr_fields)
        self.assertEqual(qr_fields["expires_at"], 120)
        self.assertEqual(account_fields["wxid"], "wxid_runtime_001")
        self.assertEqual(account_fields["nickname"], "运行号")
        self.assertEqual(account_fields["alias"], "abot_runtime")
        self.assertEqual(account_fields["bind_mobile"], "13800001111")

    def test_wechat_ipad_success_and_error_parsing_aligns_with_abot_client(self):
        success_payload = {
            "Success": True,
            "Code": 0,
            "Message": "登陆成功",
            "Data": {"acctSectResp": {"userName": "wxid_runtime_001"}},
        }
        compat_payload = {"Ret": 0, "Data": {"Synckey": "sync-key-001"}}
        failure_payload = {"Success": False, "Code": -11}

        self.assertTrue(WechatIpadApiClient.is_success(success_payload))
        self.assertTrue(WechatIpadApiClient.is_success(compat_payload))
        self.assertFalse(WechatIpadApiClient.is_success(failure_payload))
        self.assertEqual(WechatIpadApiClient.extract_code(failure_payload), "-11")
        self.assertEqual(WechatIpadApiClient.describe_error_code("-11"), "login_abnormal")
        self.assertEqual(WechatIpadApiClient.extract_message(failure_payload), "login_abnormal")

    def test_wechat_ipad_client_exposes_extended_abot_api_shapes(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            client = WechatIpadApiClient()

            with patch.object(client, "request_json", return_value={"Success": True}) as mocked:
                client.send_app_message(
                    channel,
                    wxid="wxid_runtime_001",
                    to_wxid="wxid_target_001",
                    xml="<appmsg />",
                    app_type=74,
                )
                mocked.assert_called_with(
                    channel,
                    IPAD_874_CONTRACT.endpoints.message_send_app,
                    body={"Wxid": "wxid_runtime_001", "ToWxid": "wxid_target_001", "Xml": "<appmsg />", "Type": 74},
                )

            with patch.object(client, "request_json", return_value={"Success": True}) as mocked:
                client.get_contact_detail(
                    channel,
                    wxid="wxid_runtime_001",
                    to_wxids=["wxid_a", "wxid_b"],
                    chatroom_id="room-001@chatroom",
                )
                mocked.assert_called_with(
                    channel,
                    IPAD_874_CONTRACT.endpoints.friend_get_detail,
                    body={"Wxid": "wxid_runtime_001", "Towxids": "wxid_a,wxid_b", "ChatRoom": "room-001@chatroom"},
                )

            with patch.object(client, "request_json", return_value={"Success": True}) as mocked:
                client.get_chatroom_qrcode(
                    channel,
                    wxid="wxid_runtime_001",
                    chatroom_id="room-001@chatroom",
                )
                mocked.assert_called_with(
                    channel,
                    IPAD_874_CONTRACT.endpoints.group_get_qrcode,
                    body={"Wxid": "wxid_runtime_001", "QID": "room-001@chatroom"},
                )

    def test_wechat_ipad_status_sync_persists_error_context_for_failed_heartbeat(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            with patch(
                "app.services.wechat_ipad.client.WechatIpadApiClient.request_json",
                side_effect=[
                    {
                        "Ret": 1,
                        "Msg": "heartbeat down",
                        "http_status": 500,
                    },
                    {
                        "Ret": 0,
                        "Msg": "heartbeat long ok",
                        "http_status": 200,
                    },
                    {
                        "Ret": 0,
                        "Data": {
                            "Wxid": "wxid_demo_account_001",
                            "NickName": "Runtime User",
                        },
                        "http_status": 200,
                    },
                ],
            ):
                overview = refresh_wechat_ipad_status(db, channel)

            runtime_info = json.loads(channel.runtime_info_json or "{}")
            context = runtime_info.get("last_error_context")
            self.assertEqual(runtime_info.get("last_error"), "heartbeat down")
            self.assertIsInstance(context, dict)
            self.assertEqual(context.get("stage"), "heartbeat")
            self.assertEqual(context.get("endpoint"), IPAD_874_CONTRACT.endpoints.login_heartbeat)
            self.assertTrue(context.get("at"))
            self.assertEqual(channel.status, "degraded")
            self.assertEqual(overview["last_error_stage"], "heartbeat")
            self.assertEqual(overview["last_error_endpoint"], IPAD_874_CONTRACT.endpoints.login_heartbeat)

    def test_wechat_ipad_overview_exposes_error_context(self):
        reference_time = datetime(2026, 3, 23, 12, 40, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_error": "heartbeat down",
                    "last_error_context": {
                        "stage": "heartbeat",
                        "endpoint": "/api/HeartBeat",
                        "at": "2026-03-23T12:34:56+00:00",
                    },
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        self.assertEqual(overview["last_error"], "heartbeat down")
        self.assertEqual(overview["last_error_stage"], "heartbeat")
        self.assertEqual(overview["last_error_endpoint"], "/api/HeartBeat")
        self.assertEqual(overview["last_error_at"], "2026-03-23T12:34:56+00:00")
        self.assertIn("2026-03-23", overview["last_error_at_display"])
        self.assertEqual(overview["last_error_context"]["freshness_key"], "fresh")
        self.assertEqual(overview["last_error_context"]["freshness_label"], "新鲜")
        self.assertIn("HeartBeat阶段", overview["last_error_operator_summary"])
        self.assertIn("时效 新鲜", overview["last_error_operator_summary"])
        self.assertIn("/api/HeartBeat", overview["last_error_operator_summary"])
        self.assertIn("heartbeat down", overview["last_error_operator_summary"])
        self.assertEqual(overview["error_diagnostics"]["state_label"], "运行态异常")
        self.assertEqual(overview["error_diagnostics"]["category"], "运行态检查")
        self.assertIn("HeartBeat、CacheInfo", overview["error_diagnostics"]["operator_hint"])
        self.assertEqual(overview["error_operator_snapshot"]["status_label"], "运行态异常")
        self.assertEqual(overview["error_operator_snapshot"]["headline"], "当前错误态：运行态异常")
        self.assertIn("运行态探活或重连链路", overview["error_operator_snapshot"]["summary"])
        self.assertEqual(overview["error_operator_snapshot"]["attention_label"], "优先处理")
        self.assertIn("立即核对对应接口与最新回包", overview["error_operator_snapshot"]["attention_action"])
        self.assertIn("阶段=HeartBeat", overview["error_operator_snapshot"]["detail"])
        self.assertIn("接口=/api/HeartBeat", overview["error_operator_snapshot"]["detail"])
        self.assertIn("时效=新鲜", overview["error_operator_snapshot"]["detail"])
        self.assertIn("处置=优先处理", overview["error_operator_snapshot"]["detail"])
        self.assertIn("最近错误=heartbeat down", overview["error_operator_snapshot"]["detail"])

    def test_wechat_ipad_overview_builds_error_operator_snapshot_empty_state(self):
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps({}, ensure_ascii=False)
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel)

        self.assertEqual(overview["error_operator_snapshot"]["status_key"], "no_error")
        self.assertEqual(overview["error_operator_snapshot"]["status_label"], "当前无错误")
        self.assertEqual(overview["error_operator_snapshot"]["headline"], "当前错误态：当前无错误")
        self.assertEqual(overview["error_operator_snapshot"]["attention_label"], "无需处理")
        self.assertIn("保持常规观察即可", overview["error_operator_snapshot"]["attention_action"])
        self.assertIn("最近没有记录到错误", overview["error_operator_snapshot"]["summary"])
        self.assertIn("最近错误=-", overview["error_operator_snapshot"]["detail"])
        self.assertIn("处置=无需处理", overview["error_operator_snapshot"]["detail"])
        self.assertIn("时效=尚无时间样本", overview["error_operator_snapshot"]["detail"])

    def test_wechat_ipad_overview_top_priority_points_to_recent_error_first(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.login_state = "online"
            channel.qrcode_status = "success"
            channel.last_login_at = datetime.fromisoformat("2026-03-24T11:30:00+00:00")
            channel.last_message_sync_at = datetime.fromisoformat("2026-03-24T11:52:00+00:00")
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-24T11:56:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "last_error": "heartbeat down",
                    "last_error_context": {
                        "stage": "heartbeat",
                        "endpoint": "/api/HeartBeat",
                        "at": "2026-03-24T11:58:00+00:00",
                    },
                    "last_check_qr_at": "2026-03-24T11:57:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "登陆成功", "http_status": 200},
                    "last_heartbeat_response": {"Ret": 0, "Msg": "heartbeat ok", "http_status": 200},
                    "last_heartbeat_long_at": "2026-03-24T11:55:00+00:00",
                    "last_heartbeat_long_response": {"Ret": 0, "Msg": "heartbeat long ok", "http_status": 200},
                    "last_cache_info_at": "2026-03-24T11:54:00+00:00",
                    "last_cache_info_response": {
                        "Ret": 0,
                        "Data": {"Wxid": "wxid-top-priority-001", "NickName": "Priority Ops", "DeviceID": "device-priority-001"},
                        "http_status": 200,
                    },
                    "cache_info_summary": {
                        "wxid": "wxid-top-priority-001",
                        "nickname": "Priority Ops",
                        "device_id": "device-priority-001",
                    },
                    "sync_key": "sync-key-priority-001",
                    "sync_continue_flag": 0,
                    "last_message_sync_count": 4,
                    "last_message_sync_ingested": 2,
                    "last_message_sync_deduplicated": 2,
                    "last_receive_at": "2026-03-24T11:57:30+00:00",
                    "last_receive_summary": "收到 inbound 回调，已完成标准化。",
                    "last_send_at": "2026-03-24T11:56:30+00:00",
                    "last_send_summary": "发送 text 到 room-ops，回执 delivered。",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        snapshot = overview["top_priority_snapshot"]
        self.assertEqual(snapshot["module_label"], "最近错误")
        self.assertEqual(snapshot["status_key"], "focus_error")
        self.assertIn("当前优先处理最近错误", snapshot["summary"])
        self.assertIn("最近错误时效=新鲜", snapshot["basis_summary"])
        self.assertIn("错误分类偏向运行态异常", snapshot["basis_summary"])
        self.assertIn("HeartBeat阶段已出现失败样本", snapshot["basis_summary"])
        self.assertIn("阶段=HeartBeat", snapshot["evidence"])
        self.assertIn("立即核对对应接口与最新回包", snapshot["next_action"])
        self.assertIn("判定依据=最近错误时效=新鲜", snapshot["operator_summary"])
        self.assertIn("当前优先项=最近错误", snapshot["operator_summary"])

    def test_wechat_ipad_overview_top_priority_stays_low_risk_when_runtime_is_stable(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        group_summary = {
            "stage_snapshot": {
                "status_key": "member_records_available",
                "status_label": "已有成员记录",
                "status_tone": "success",
                "freshness_key": "fresh",
                "operator_summary": "群资料样本已形成基本闭环。",
                "action": "继续观察群资料同步时效。",
            },
            "group_plugin_governance_snapshot": {
                "status_label": "尚未具备",
                "status_tone": "muted",
                "focus_label": "先补真实样本",
                "next_step_summary": "继续补真实样本。",
                "action": "继续补真实样本。",
            },
        }
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.login_state = "online"
            channel.qrcode_status = "success"
            channel.last_login_at = datetime.fromisoformat("2026-03-24T11:40:00+00:00")
            channel.last_message_sync_at = datetime.fromisoformat("2026-03-24T11:54:00+00:00")
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-24T11:56:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "last_check_qr_at": "2026-03-24T11:57:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "登陆成功", "http_status": 200},
                    "last_heartbeat_response": {"Ret": 0, "Msg": "heartbeat ok", "http_status": 200},
                    "last_heartbeat_long_at": "2026-03-24T11:55:00+00:00",
                    "last_heartbeat_long_response": {"Ret": 0, "Msg": "heartbeat long ok", "http_status": 200},
                    "last_cache_info_at": "2026-03-24T11:54:30+00:00",
                    "last_cache_info_response": {
                        "Ret": 0,
                        "Data": {"Wxid": "wxid-stable-001", "NickName": "Stable Ops", "DeviceID": "device-stable-001"},
                        "http_status": 200,
                    },
                    "cache_info_summary": {
                        "wxid": "wxid-stable-001",
                        "nickname": "Stable Ops",
                        "device_id": "device-stable-001",
                    },
                    "sync_key": "sync-key-stable-001",
                    "sync_continue_flag": 0,
                    "last_message_sync_count": 3,
                    "last_message_sync_ingested": 1,
                    "last_message_sync_deduplicated": 2,
                    "last_receive_at": "2026-03-24T11:58:00+00:00",
                    "last_receive_summary": "收到 wechat_ipad_sync 事件，event_key=evt-stable-receive，已入处理链路。",
                    "last_inbound_protocol_summary": {"source": "wechat_ipad_sync", "event_key": "evt-stable-receive"},
                    "last_send_at": "2026-03-24T11:57:00+00:00",
                    "last_send_summary": "发送 text 到 room-stable，回执 delivered。",
                    "last_outbound_protocol_summary": {"receiver_id": "room-stable"},
                    "receive_total": 5,
                    "dedupe_total": 1,
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time, group_summary=group_summary)

        snapshot = overview["top_priority_snapshot"]
        self.assertEqual(snapshot["module_label"], "持续观察")
        self.assertEqual(snapshot["issue_label"], "无高优先异常")
        self.assertEqual(snapshot["status_tone"], "success")
        self.assertIn("当前无高优先异常", snapshot["summary"])
        self.assertIn("登录、协议、同步、收发当前都未见更高优先异常", snapshot["basis_summary"])
        self.assertIn("当前更适合继续补真实样本并观察下一次同步", snapshot["basis_summary"])
        self.assertIn("群级治理当前更像先补真实样本", snapshot["basis_summary"])
        self.assertIn("继续补真实样本并观察下一次同步", snapshot["next_action"])
        self.assertIn("判定依据=登录、协议、同步、收发当前都未见更高优先异常", snapshot["operator_summary"])
        self.assertIn("当前优先项=持续观察", snapshot["operator_summary"])

    def test_wechat_ipad_overview_marks_expired_error_as_historical_sample(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.runtime_info_json = json.dumps(
                {
                    "last_error": "sync timeout once",
                    "last_error_context": {
                        "stage": "message_sync",
                        "endpoint": "/api/Msg/Sync",
                        "at": "2026-03-24T09:30:00+00:00",
                    },
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        self.assertEqual(overview["last_error_context"]["freshness_label"], "过旧")
        self.assertEqual(overview["error_operator_snapshot"]["attention_label"], "历史样本")
        self.assertIn("先刷新运行态与同步样本", overview["error_operator_snapshot"]["attention_action"])
        self.assertIn("处置=历史样本", overview["error_operator_snapshot"]["detail"])

    def test_wechat_ipad_overview_expands_error_classification_for_login_runtime_and_sync_variants(self):
        cases = [
            {
                "name": "登录确认阶段",
                "runtime_info": {
                    "last_error": "登录确认超时",
                    "last_error_context": {
                        "stage": "login_confirm",
                        "endpoint": IPAD_874_CONTRACT.endpoints.login_check_qr,
                        "at": "2026-03-24T01:02:03+00:00",
                    },
                },
                "expected_state": "登录态异常",
                "expected_category": "登录流程",
                "expected_stage_label": "登录确认阶段",
                "expected_hint": "扫码或登录确认链路",
            },
            {
                "name": "自动登录阶段",
                "runtime_info": {
                    "last_error": "auto auth denied",
                    "last_error_context": {
                        "stage": "auto_login",
                        "endpoint": IPAD_874_CONTRACT.endpoints.login_twice_auto_auth,
                        "at": "2026-03-24T04:05:06+00:00",
                    },
                },
                "expected_state": "运行态异常",
                "expected_category": "运行态检查",
                "expected_stage_label": "自动登录阶段",
                "expected_hint": "自动登录链路",
            },
            {
                "name": "同步接口兜底识别",
                "runtime_info": {
                    "last_error": "sync key expired",
                    "last_error_context": {
                        "stage": "",
                        "endpoint": "/api/Msg/Sync",
                        "at": "2026-03-24T07:08:09+00:00",
                    },
                },
                "expected_state": "同步异常",
                "expected_category": "消息同步",
                "expected_stage_label": None,
                "expected_hint": "Msg/Sync 回包",
            },
            {
                "name": "重连阶段别名",
                "runtime_info": {
                    "last_error": "reconnect pending",
                    "last_error_context": {
                        "stage": "reconnect_probe",
                        "endpoint": "/api/Login/Reconnect",
                        "at": "2026-03-24T08:08:09+00:00",
                    },
                },
                "expected_state": "运行态异常",
                "expected_category": "运行态检查",
                "expected_stage_label": "重连恢复阶段",
                "expected_hint": "重连唤醒链路",
            },
            {
                "name": "唤醒接口变体",
                "runtime_info": {
                    "last_error": "awake timeout",
                    "last_error_context": {
                        "stage": "awake",
                        "endpoint": IPAD_874_CONTRACT.endpoints.login_awaken,
                        "at": "2026-03-24T08:18:09+00:00",
                    },
                },
                "expected_state": "运行态异常",
                "expected_category": "运行态检查",
                "expected_stage_label": "Awaken阶段",
                "expected_hint": "重连唤醒链路",
            },
            {
                "name": "等待确认阶段别名",
                "runtime_info": {
                    "last_error": "wait confirm timeout",
                    "last_error_context": {
                        "stage": "wait_confirm",
                        "endpoint": "/api/Login/Confirm",
                        "at": "2026-03-24T08:28:09+00:00",
                    },
                },
                "expected_state": "登录态异常",
                "expected_category": "登录流程",
                "expected_stage_label": "登录确认阶段",
                "expected_hint": "扫码或登录确认链路",
            },
            {
                "name": "联系人同步阶段别名",
                "runtime_info": {
                    "last_error": "contact sync denied",
                    "last_error_context": {
                        "stage": "sync_contacts",
                        "endpoint": "/api/Contact/Sync",
                        "at": "2026-03-24T08:38:09+00:00",
                    },
                },
                "expected_state": "同步异常",
                "expected_category": "消息同步",
                "expected_stage_label": "联系人同步阶段",
                "expected_hint": "资料同步链路",
            },
            {
                "name": "群资料同步阶段别名",
                "runtime_info": {
                    "last_error": "chatroom members stale",
                    "last_error_context": {
                        "stage": "sync_chatroom_members",
                        "endpoint": "/api/ChatRoom/Sync",
                        "at": "2026-03-24T08:48:09+00:00",
                    },
                },
                "expected_state": "同步异常",
                "expected_category": "消息同步",
                "expected_stage_label": "群资料同步阶段",
                "expected_hint": "资料同步链路",
            },
        ]

        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            for case in cases:
                with self.subTest(case["name"]):
                    channel.runtime_info_json = json.dumps(case["runtime_info"], ensure_ascii=False)
                    db.commit()
                    db.refresh(channel)

                    overview = build_wechat_ipad_overview(channel)

                    self.assertEqual(overview["error_diagnostics"]["state_label"], case["expected_state"])
                    self.assertEqual(overview["error_diagnostics"]["category"], case["expected_category"])
                    self.assertIn(case["expected_hint"], overview["error_diagnostics"]["operator_hint"])
                    if case["expected_stage_label"]:
                        self.assertIn(case["expected_stage_label"], overview["last_error_operator_summary"])
                        self.assertIn(
                            f"阶段={case['expected_stage_label'].removesuffix('阶段')}",
                            overview["error_operator_snapshot"]["detail"],
                        )
                    else:
                        self.assertNotIn("待归类异常", overview["error_diagnostics"]["state_label"])

    def test_wechat_ipad_channel_detail_shows_error_operator_summary(self):
        reference_time = datetime(2026, 3, 23, 12, 54, 56, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
            channel.runtime_info_json = json.dumps(
                {
                    "last_error": "heartbeat down",
                    "last_error_context": {
                        "stage": "heartbeat",
                        "endpoint": "/api/Login/HeartBeat?wxid=wxid_ops_summary",
                        "at": "2026-03-23T12:34:56+00:00",
                    },
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            overview = build_wechat_ipad_overview(channel, now=reference_time)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=overview,
                wechat_ipad_group_summary=None,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("当前错误态：运行态异常", html)
        self.assertIn("关键信息：分类=运行态异常", html)
        self.assertIn("最近错误运维摘要", html)
        self.assertIn("HeartBeat阶段", html)
        self.assertIn("/api/Login/HeartBeat?wxid=wxid_ops_summary", html)
        self.assertIn("heartbeat down", html)
        self.assertIn("最近错误分类", html)
        self.assertIn("运行态异常", html)
        self.assertIn("最近错误处置", html)
        self.assertIn("先刷新再判断", html)
        self.assertIn("下一步：最近错误样本已偏旧", html)
        self.assertIn("最近错误建议", html)
        self.assertIn("运行态探活或重连链路", html)
        self.assertIn("最近错误时效", html)
        self.assertIn('最近错误时效</th><td><span class="chip chip-outline">偏旧</span> 按最近错误时间分档', html)

    def test_wechat_ipad_channel_detail_shows_recent_call_freshness_chips(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-24T11:30:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "last_check_qr_at": "2026-03-24T11:55:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                    "last_heartbeat_response": {"Ret": 0, "Msg": "heartbeat ok", "http_status": 200},
                    "last_heartbeat_long_at": "2026-03-24T10:30:00+00:00",
                    "last_heartbeat_long_response": {"Ret": 0, "Msg": "heartbeat long ok", "http_status": 200},
                    "last_cache_info_response": {"Ret": 0, "Data": {}, "http_status": 200},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            overview = build_wechat_ipad_overview(channel, now=reference_time)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=overview,
                wechat_ipad_group_summary=None,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn('CheckQR 时效</th><td><span class="chip chip-success">新鲜</span> 按最近调用时间分档', html)
        self.assertIn('HeartBeat 时效</th><td><span class="chip chip-outline">偏旧</span> 按最近调用时间分档', html)
        self.assertIn('HeartBeatLong 时效</th><td><span class="chip chip-outline">过旧</span> 按最近调用时间分档', html)
        self.assertIn('CacheInfo 时效</th><td><span class="chip chip-muted">尚无时间样本</span> 暂无最近调用时间样本', html)

    def test_wechat_ipad_channel_detail_shows_recent_protocol_operator_snapshot(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            bots = db.scalars(select(Bot).order_by(Bot.id.desc())).all()
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-24T11:50:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "last_check_qr_at": "2026-03-24T11:58:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                    "last_heartbeat_response": {"Ret": 0, "Msg": "heartbeat ok", "http_status": 200},
                    "last_heartbeat_long_at": "2026-03-24T10:40:00+00:00",
                    "last_heartbeat_long_response": {"Ret": 0, "Msg": "heartbeat long ok", "http_status": 200},
                    "last_cache_info_response": {"Ret": 1, "Msg": "cache offline", "http_status": 502},
                    "last_cache_info_at": "2026-03-24T11:45:00+00:00",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)
            overview = build_wechat_ipad_overview(channel, now=reference_time)
            request = Request({"type": "http", "method": "GET", "path": "/channels/1", "headers": []})
            html = templates.get_template("channel_detail.html").render(
                request=request,
                page_title="渠道详情 · 微信渠道",
                channel=channel,
                bots=bots,
                token_preview=channel.bot_token_masked or "-",
                health_checks={
                    "api_base": bool(channel.api_base),
                    "webhook_path": bool(channel.webhook_path),
                    "bot_token": bool(channel.bot_token_encrypted),
                    "adapter_config": bool(channel.adapter_config_json),
                },
                channel_health_timeline={
                    "latest": None,
                    "latest_success_at": None,
                    "latest_failure_at": None,
                    "latest_success_summary": "",
                    "latest_failure_summary": "",
                },
                wechat_ipad_overview=overview,
                wechat_ipad_group_summary=None,
                current_user={"display_name": "ops", "role": "ops"},
                nav_items=[("/", "/", "总览"), ("/channels", "/channels", "渠道")],
                notice="",
            )

        self.assertIn("最近协议调用总览", html)
        self.assertIn("最近协议调用：部分失败", html)
        self.assertIn("先看失败探针", html)
        self.assertIn("CacheInfo调用失败", html)
        self.assertIn("CheckQR调用成功", html)
        self.assertIn("HeartBeatLong调用成功", html)
        self.assertIn("<span class=\"chip chip-outline\">过旧</span>", html)
        self.assertIn("下一步：先查看失败探针的 HTTP 状态、返回码和最近错误上下文", html)
        self.assertIn("结构化摘要：总览=存在失败", html)

    def test_wechat_ipad_overview_builds_recent_protocol_operator_snapshot(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-24T11:40:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "last_check_qr_at": "2026-03-24T11:57:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                    "last_heartbeat_response": {"Ret": 0, "Msg": "heartbeat ok", "http_status": 200},
                    "last_heartbeat_long_at": "2026-03-24T10:45:00+00:00",
                    "last_heartbeat_long_response": {"Ret": 0, "Msg": "heartbeat long ok", "http_status": 200},
                    "last_cache_info_response": {"Ret": 1, "Msg": "cache offline", "http_status": 502},
                    "last_cache_info_at": "2026-03-24T11:35:00+00:00",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        snapshot = overview["protocol_state"]["operator_snapshot"]
        self.assertEqual(snapshot["status_key"], "failed")
        self.assertEqual(snapshot["status_label"], "存在失败")
        self.assertEqual(snapshot["headline"], "最近协议调用：部分失败")
        self.assertEqual(snapshot["focus_label"], "先看失败探针")
        self.assertIn("失败探针：CacheInfo", snapshot["summary"])
        self.assertIn("下一眼=先看失败探针", snapshot["operator_summary"])
        self.assertIn("HeartBeatLong", snapshot["detail"])
        self.assertIn("样本不足=-", snapshot["operator_summary"])
        self.assertEqual(len(snapshot["items"]), 4)
        self.assertEqual(snapshot["items"][0]["label"], "CheckQR")
        self.assertEqual(snapshot["items"][0]["summary"], "调用成功 / 新鲜")
        self.assertIn("CheckQR=调用成功", snapshot["items"][0]["operator_summary"])
        self.assertEqual(snapshot["items"][3]["label"], "CacheInfo")
        self.assertEqual(snapshot["items"][3]["state_label"], "调用失败")
        self.assertEqual(snapshot["items"][3]["freshness_label"], "偏旧")
        self.assertIn("CacheInfo=调用失败", snapshot["items"][3]["operator_summary"])

    def test_wechat_ipad_overview_builds_top_priority_snapshot_for_recent_error(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-24T11:55:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "last_error": "heartbeat down",
                    "last_error_context": {
                        "stage": "heartbeat",
                        "endpoint": "/api/Login/HeartBeat?wxid=wxid_top_priority",
                        "at": "2026-03-24T11:58:00+00:00",
                    },
                    "last_check_qr_at": "2026-03-24T11:57:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "等待扫码", "http_status": 200},
                    "last_heartbeat_response": {"Ret": 1, "Msg": "heartbeat down", "http_status": 502},
                    "last_cache_info_at": "2026-03-24T11:56:00+00:00",
                    "last_cache_info_response": {"Ret": 0, "Data": {"wxid": "wxid_top_priority"}, "http_status": 200},
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        snapshot = overview["top_priority_snapshot"]
        self.assertEqual(snapshot["status_key"], "focus_error")
        self.assertEqual(snapshot["module_label"], "最近错误")
        self.assertEqual(snapshot["issue_label"], "运行态异常")
        self.assertIn("当前首要处理项：最近错误", snapshot["headline"])
        self.assertIn("最近错误", snapshot["summary"])
        self.assertEqual(snapshot["confidence_label"], "高置信度当前故障")
        self.assertIn("当前故障信号明确", snapshot["confidence_summary"])
        self.assertIn("HeartBeat", snapshot["evidence"])
        self.assertIn("核对", snapshot["next_action"])
        self.assertIn("当前把握=高置信度当前故障", snapshot["operator_summary"])
        self.assertIn("当前优先项=最近错误", snapshot["operator_summary"])

    def test_wechat_ipad_overview_builds_top_priority_snapshot_for_stable_state(self):
        reference_time = datetime(2026, 3, 24, 12, 0, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            channel = db.get(Channel, 1)
            self.assertIsNotNone(channel)
            channel.login_state = "online"
            channel.qrcode_status = "confirmed"
            channel.last_login_at = datetime.fromisoformat("2026-03-24T11:40:00+00:00")
            channel.last_heartbeat_at = datetime.fromisoformat("2026-03-24T11:55:00+00:00")
            channel.last_message_sync_at = datetime.fromisoformat("2026-03-24T11:53:00+00:00")
            channel.runtime_info_json = json.dumps(
                {
                    "last_check_qr_at": "2026-03-24T11:58:00+00:00",
                    "last_check_qr_response": {"Ret": 0, "Msg": "二维码已确认", "http_status": 200},
                    "last_heartbeat_response": {"Ret": 0, "Msg": "heartbeat ok", "http_status": 200},
                    "last_heartbeat_long_at": "2026-03-24T11:54:00+00:00",
                    "last_heartbeat_long_response": {"Ret": 0, "Msg": "heartbeat long ok", "http_status": 200},
                    "last_cache_info_at": "2026-03-24T11:52:00+00:00",
                    "last_cache_info_response": {
                        "Ret": 0,
                        "Data": {"wxid": "wxid_ok", "nickname": "ops", "device_id": "dev-ok"},
                        "http_status": 200,
                    },
                    "last_receive_at": "2026-03-24T11:57:00+00:00",
                    "last_send_at": "2026-03-24T11:56:00+00:00",
                    "receive_total": 12,
                    "dedupe_total": 1,
                    "last_message_sync_count": 3,
                    "last_message_sync_ingested": 3,
                    "last_message_sync_deduplicated": 0,
                    "last_pull_protocol_summary": "最近拉取成功，新增 3 条",
                    "last_sync_summary": "最近同步正常",
                },
                ensure_ascii=False,
            )
            db.commit()
            db.refresh(channel)

            overview = build_wechat_ipad_overview(channel, now=reference_time)

        snapshot = overview["top_priority_snapshot"]
        self.assertEqual(snapshot["status_key"], "healthy")
        self.assertEqual(snapshot["module_label"], "持续观察")
        self.assertEqual(snapshot["issue_label"], "无高优先异常")
        self.assertEqual(snapshot["status_tone"], "success")
        self.assertIn("当前首要处理项：持续观察", snapshot["headline"])
        self.assertIn("当前无高优先异常", snapshot["summary"])
        self.assertEqual(snapshot["confidence_label"], "继续观察")
        self.assertIn("继续观察为主", snapshot["confidence_summary"])
        self.assertIn("登录=", snapshot["evidence"])
        self.assertIn("协议=", snapshot["evidence"])
        self.assertIn("同步=", snapshot["evidence"])
        self.assertIn("收发=", snapshot["evidence"])
        self.assertTrue(snapshot["next_action"])
        self.assertNotIn("高置信度当前故障", snapshot["operator_summary"])
        self.assertIn("当前优先项=持续观察", snapshot["operator_summary"])

    def test_wechat_ipad_channel_detail_template_wires_structured_sync_operator_summary(self):
        template_text = Path("app/templates/channel_detail.html").read_text(encoding="utf-8")
        self.assertIn("结构化运维摘要", template_text)
        self.assertIn("wechat_ipad_overview.sync_diagnostics.operator_summary", template_text)
        self.assertIn("wechat_ipad_overview.sync_diagnostics.operator_snapshot.headline", template_text)
        self.assertIn("wechat_ipad_overview.sync_diagnostics.operator_snapshot.summary", template_text)
        self.assertIn("wechat_ipad_overview.error_operator_snapshot.headline", template_text)
        self.assertIn("wechat_ipad_overview.error_operator_snapshot.summary", template_text)
        self.assertIn("wechat_ipad_overview.error_operator_snapshot.detail", template_text)
        self.assertIn("wechat_ipad_overview.error_operator_snapshot.attention_label", template_text)
        self.assertIn("wechat_ipad_overview.error_operator_snapshot.attention_action", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.operator_snapshot.headline", template_text)
        self.assertIn("最近协议调用总览", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.operator_snapshot.focus_label", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.operator_snapshot.summary", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.operator_snapshot.detail", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.operator_snapshot.action", template_text)
        self.assertIn('wechat_ipad_overview.protocol_state.operator_snapshot["items"]', template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.operator_snapshot.operator_summary", template_text)
        self.assertIn("最近错误处置", template_text)
        self.assertIn("最近错误时效", template_text)
        self.assertIn("wechat_ipad_overview.last_error_context.freshness_label", template_text)
        self.assertIn("CheckQR 时效", template_text)
        self.assertIn("CheckQR 最近调用时间", template_text)
        self.assertIn("CheckQR 运维摘要", template_text)
        self.assertIn("HeartBeat 时效", template_text)
        self.assertIn("HeartBeat 最近调用时间", template_text)
        self.assertIn("HeartBeat 运维摘要", template_text)
        self.assertIn("HeartBeatLong 时效", template_text)
        self.assertIn("HeartBeatLong 最近调用时间", template_text)
        self.assertIn("CacheInfo 时效", template_text)
        self.assertIn("wechat_ipad_overview.current_ops_snapshot.headline", template_text)
        self.assertIn("wechat_ipad_overview.current_ops_snapshot.evidence", template_text)
        self.assertIn("wechat_ipad_overview.current_ops_snapshot.action", template_text)
        self.assertIn('wechat_ipad_overview.current_ops_snapshot["items"]', template_text)
        self.assertIn("wechat_ipad_overview.current_ops_snapshot.operator_summary", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.headline", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.summary", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.confidence_label", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.confidence_summary", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.basis_summary", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.evidence", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.next_action", template_text)
        self.assertIn("wechat_ipad_overview.top_priority_snapshot.operator_summary", template_text)
        self.assertIn("Top Priority", template_text)
        self.assertIn('<p class="section-kicker" style="margin:0 0 4px;">Top Priority</p>', template_text)
        self.assertIn("当前把握：{{ tone_chip(wechat_ipad_overview.top_priority_snapshot.confidence_label, wechat_ipad_overview.top_priority_snapshot.confidence_tone) }} {{ wechat_ipad_overview.top_priority_snapshot.confidence_summary }}", template_text)
        self.assertIn("判定依据：{{ wechat_ipad_overview.top_priority_snapshot.basis_summary }}", template_text)
        self.assertIn("关键证据：{{ wechat_ipad_overview.top_priority_snapshot.evidence }}", template_text)
        self.assertIn("下一步：{{ wechat_ipad_overview.top_priority_snapshot.next_action }}", template_text)
        self.assertIn("wechat_ipad_group_summary.ops_snapshot.headline", template_text)
        self.assertIn("wechat_ipad_group_summary.ops_snapshot.evidence", template_text)
        self.assertIn("wechat_ipad_group_summary.ops_snapshot.action", template_text)
        self.assertIn("wechat_ipad_group_summary.summary_snapshot.decision_label", template_text)
        self.assertIn("wechat_ipad_group_summary.summary_snapshot.operator_summary", template_text)
        self.assertIn("wechat_ipad_overview.recent_message_flow.operator_snapshot.evidence", template_text)
        self.assertIn('wechat_ipad_overview.recent_message_flow.operator_snapshot["items"]', template_text)
        self.assertIn("wechat_ipad_overview.recent_message_flow.operator_snapshot.action", template_text)
        self.assertIn("wechat_ipad_group_summary.stage_snapshot.operator_summary", template_text)
        self.assertIn("wechat_ipad_group_summary.stage_snapshot.freshness_label", template_text)
        self.assertIn("wechat_ipad_group_summary.plugin_scope_snapshot.stage_label", template_text)
        self.assertIn("wechat_ipad_overview.group_plugin_governance_snapshot.single_overview_summary", template_text)
        self.assertIn("wechat_ipad_group_summary.plugin_scope_snapshot.boundary_note", template_text)
        self.assertIn("detail_group_allow_deny_model_snapshot", template_text)
        self.assertIn("group_allow_deny_model_snapshot", template_text)
        self.assertIn("群级 allow/deny 口径", template_text)
        self.assertIn("detail_group_allow_deny_model_snapshot.recommended_dimensions", template_text)
        self.assertIn("detail_group_allow_deny_model_snapshot.boundary_note", template_text)
        self.assertIn("CacheInfo 最近调用时间", template_text)
        self.assertIn("CacheInfo 运维摘要", template_text)
        self.assertIn("tone_chip(wechat_ipad_overview.protocol_state.last_check_qr_result.freshness_label", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_heartbeat_result.freshness_label", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_heartbeat_long_result.freshness_label", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_cache_info_result.freshness_label", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_check_qr_result.called_at_display", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_heartbeat_result.called_at_display", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_heartbeat_long_result.called_at_display", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_cache_info_result.called_at_display", template_text)
        self.assertIn("wechat_ipad_overview.recent_message_flow.operator_snapshot.freshness_label", template_text)
        self.assertIn("wechat_ipad_overview.recent_message_flow.operator_snapshot.focus_label", template_text)
        self.assertIn("wechat_ipad_overview.recent_message_flow.operator_snapshot.summary", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_check_qr_result.operator_summary", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_heartbeat_result.operator_summary", template_text)
        self.assertIn("wechat_ipad_overview.protocol_state.last_cache_info_result.operator_summary", template_text)
        self.assertIn("Wechat iPad 运维动作台", template_text)
        self.assertIn("wechat_ipad_overview.action_console.supported_actions", template_text)
        self.assertIn("data-action-endpoint-template", template_text)
        self.assertIn("wechat-ipad-action-run", template_text)
        self.assertIn("wechat-ipad-action-last-preview", template_text)

    def test_login_and_role_permissions(self):
        login_page = self.client.get("/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertNotIn("admin123", login_page.text)
        self.assertNotIn("ops123", login_page.text)
        self.assertNotIn("viewer123", login_page.text)
        self.assertNotIn("验证码", login_page.text)

        page = self.client.get("/", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertIn("/login", page.headers["location"])

        login_response = self.login("viewer", "viewer123")
        cookie_header = login_response.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("Max-Age=43200", cookie_header)
        self.assertIn("SameSite=lax", cookie_header)

        readonly_page = self.client.get("/dashboard")
        self.assertEqual(readonly_page.status_code, 200)

        users_page = self.client.get("/users")
        self.assertEqual(users_page.status_code, 403)

        forbidden = self.client.post(
            "/channels/1/health-check",
            follow_redirects=False,
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_workwechat_provider_register_is_not_blocked_by_admin_login(self):
        # 企微桌面 Provider 是独立客户端，启动注册时没有后台管理端 Cookie；
        # 这里必须进入 provider 自身的授权码校验逻辑，而不是被后台登录中间件重定向到 /login。
        response = self.client.post(
            "/api/workwechat/provider/register",
            json={"authorization_code": "wwauth-invalid"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "authorization_code_invalid")

    def test_workwechat_provider_cloud_inbound_uses_client_token(self):
        with SessionLocal() as db:
            bot = Bot(name="企微云端入站测试机器人")
            db.add(bot)
            db.flush()
            channel = Channel(bot_id=bot.id, name="企微云端入站", channel_type="workwechat")
            db.add(channel)
            db.flush()
            code = issue_authorization_code(channel)
            token = exchange_authorization_code(
                channel,
                code,
                provider_name="客服电脑A",
                client_version="5.0.0.6008",
            )["client_token"]
            channel_id = channel.id
            db.commit()

        payload = {
            "event_key": "evt-workwechat-cloud-inbound-001",
            "event_type": "message",
            "conversation": {"external_id": "R:cloud-room-001", "title": "云端企微测试群", "chat_type": "group"},
            "message": {
                "content": "云端入站 hello",
                "sender_id": "ww-user-001",
                "sender_name": "测试成员",
                "message_type": "text",
                "external_message_id": "ww-msg-001",
            },
            "details": {"source_protocol": "workwechat_hook"},
        }

        invalid = self.client.post(
            "/api/workwechat/provider/inbound",
            json=payload,
            headers={"X-WorkWechat-Client-Token": "bad-token"},
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["detail"], "invalid_client_token")

        response = self.client.post(
            "/api/workwechat/provider/inbound",
            json=payload,
            headers={"X-WorkWechat-Client-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["event_type"], "message")

        with SessionLocal() as db:
            raw_event = db.scalar(select(RawWebhookEvent).where(RawWebhookEvent.channel_id == channel_id))
            conversation = db.scalar(select(Conversation).where(Conversation.channel_id == channel_id))
            message = db.scalar(select(Message).where(Message.conversation_id == conversation.id)) if conversation else None

        self.assertIsNotNone(raw_event)
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation.external_id, "R:cloud-room-001")
        self.assertIsNotNone(message)
        self.assertEqual(message.content, "云端入站 hello")

        self_sent_payload = {
            **payload,
            "event_key": "evt-workwechat-cloud-self-001",
            "message": {
                **payload["message"],
                "content": "客服手动发出的消息",
                "sender_id": "self-user",
                "external_message_id": "ww-self-msg-001",
            },
            "details": {
                "source_protocol": "workwechat_hook",
                "is_self_sent": True,
                "direction": "out",
                "skip_plugin_dispatch": True,
            },
        }
        self_sent = self.client.post(
            "/api/workwechat/provider/inbound",
            json=self_sent_payload,
            headers={"X-WorkWechat-Client-Token": token},
        )
        self.assertEqual(self_sent.status_code, 200)
        self.assertTrue(self_sent.json()["ok"])
        self.assertEqual(self_sent.json()["dispatch"]["status"], "skipped")
        self.assertEqual(self_sent.json()["dispatch"]["reason"], "skip_plugin_dispatch")

        with SessionLocal() as db:
            self_message = db.scalar(select(Message).where(Message.content == "客服手动发出的消息"))
            self_raw = db.scalar(select(RawWebhookEvent).where(RawWebhookEvent.event_key == "evt-workwechat-cloud-self-001"))

        self.assertIsNotNone(self_message)
        self.assertEqual(self_message.direction, "out")
        self.assertIsNotNone(self_raw)
        self.assertIn("skip_plugin_dispatch", self_raw.dispatch_summary_json or "")

    def test_login_shows_captcha_only_after_repeated_failures(self):
        for _ in range(2):
            bad_login = self.client.post(
                "/login",
                data={"username": "admin", "password": "wrong-pass"},
                follow_redirects=False,
            )
            self.assertEqual(bad_login.status_code, 401)
            self.assertIn("用户名或密码错误", bad_login.text)
            self.assertNotIn("验证码", bad_login.text)

        third_bad_login = self.client.post(
            "/login",
            data={"username": "admin", "password": "wrong-pass"},
            follow_redirects=False,
        )
        self.assertEqual(third_bad_login.status_code, 401)
        self.assertIn("验证码", third_bad_login.text)

        login_page = self.client.get("/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("验证码", login_page.text)

    def test_template_pages_do_not_emit_template_response_deprecation(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")

            login_page = self.client.get("/login")
            self.assertEqual(login_page.status_code, 200)

            self.login("admin", "admin123")
            bots_page = self.client.get("/bots")
            self.assertEqual(bots_page.status_code, 200)
            self.assertIn("机器人", bots_page.text)

        template_warnings = [
            item
            for item in caught
            if issubclass(item.category, DeprecationWarning) and "TemplateResponse" in str(item.message)
        ]
        self.assertEqual(template_warnings, [])

    def test_user_management_and_password_rotation_flow(self):
        self.login("admin", "admin123")

        users_page = self.client.get("/users")
        self.assertEqual(users_page.status_code, 200)
        self.assertIn("账号管理", users_page.text)

        create_response = self.client.post(
            "/users",
            data={
                "username": "ops-night",
                "display_name": "夜班运维",
                "role": "ops",
                "password": "night123",
                "is_enabled": "true",
            },
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 303)

        users_page = self.client.get("/users")
        self.assertIn("ops-night", users_page.text)
        self.assertIn("夜班运维", users_page.text)

        with SessionLocal() as db:
            created_user = db.scalar(select(AdminUser).where(AdminUser.username == "ops-night"))
            self.assertIsNotNone(created_user)
            created_user_id = created_user.id
            admin_user = db.scalar(select(AdminUser).where(AdminUser.username == "admin"))
            self.assertIsNotNone(admin_user)
            admin_user_id = admin_user.id

        toggle_response = self.client.post(f"/users/{created_user_id}/toggle", follow_redirects=False)
        self.assertEqual(toggle_response.status_code, 303)

        with SessionLocal() as db:
            created_user = db.scalar(select(AdminUser).where(AdminUser.username == "ops-night"))
            self.assertIsNotNone(created_user)
            self.assertFalse(created_user.is_enabled)

        last_admin_guard = self.client.post(f"/users/{admin_user_id}/toggle", follow_redirects=False)
        self.assertEqual(last_admin_guard.status_code, 303)
        self.assertIn("%E4%B8%8D%E8%83%BD%E5%81%9C%E7%94%A8%E5%BD%93%E5%89%8D%E7%99%BB%E5%BD%95%E8%B4%A6%E5%8F%B7", last_admin_guard.headers["location"])

        password_page = self.client.get("/profile/password")
        self.assertEqual(password_page.status_code, 200)
        self.assertIn("修改密码", password_page.text)

        change_response = self.client.post(
            "/profile/password",
            data={
                "current_password": "admin123",
                "new_password": "admin456",
                "confirm_password": "admin456",
            },
            follow_redirects=False,
        )
        self.assertEqual(change_response.status_code, 303)
        self.assertIn("%E5%AF%86%E7%A0%81%E5%B7%B2%E6%9B%B4%E6%96%B0", change_response.headers["location"])

        self.client.post("/logout", follow_redirects=False)
        bad_login = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        self.assertEqual(bad_login.status_code, 200)
        self.assertIn("用户名或密码错误", bad_login.text)

        good_login = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin456"},
            follow_redirects=False,
        )
        self.assertEqual(good_login.status_code, 303)

    def test_password_change_invalidates_previous_cookie(self):
        self.login("admin", "admin123")
        old_cookie = self.client.cookies.get(AUTH_COOKIE_NAME)
        self.assertTrue(old_cookie)

        change_response = self.client.post(
            "/profile/password",
            data={
                "current_password": "admin123",
                "new_password": "admin456",
                "confirm_password": "admin456",
            },
            follow_redirects=False,
        )
        self.assertEqual(change_response.status_code, 303)

        self.client.cookies.clear()
        self.client.cookies.set(AUTH_COOKIE_NAME, old_cookie)
        old_session_response = self.client.get("/users", follow_redirects=False)
        self.assertEqual(old_session_response.status_code, 303)
        self.assertIn("/login", old_session_response.headers["location"])

        self.client.cookies.clear()
        bad_login = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin123"},
            follow_redirects=False,
        )
        self.assertEqual(bad_login.status_code, 200)

        good_login = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin456"},
            follow_redirects=False,
        )
        self.assertEqual(good_login.status_code, 303)

    def test_disabling_user_invalidates_existing_cookie(self):
        with SessionLocal() as db:
            target = db.scalar(select(AdminUser).where(AdminUser.username == "viewer"))
            self.assertIsNotNone(target)
            target_user_id = target.id

        self.login("viewer", "viewer123")
        old_cookie = self.client.cookies.get(AUTH_COOKIE_NAME)
        self.assertTrue(old_cookie)
        self.client.cookies.clear()

        self.login("admin", "admin123")
        toggle_response = self.client.post(f"/users/{target_user_id}/toggle", follow_redirects=False)
        self.assertEqual(toggle_response.status_code, 303)

        self.client.cookies.clear()
        self.client.cookies.set(AUTH_COOKIE_NAME, old_cookie)
        denied = self.client.get("/dashboard", follow_redirects=False)
        self.assertEqual(denied.status_code, 303)
        self.assertIn("/login", denied.headers["location"])

        login_again = self.client.post(
            "/login",
            data={"username": "viewer", "password": "viewer123"},
            follow_redirects=False,
        )
        self.assertEqual(login_again.status_code, 200)

    def test_legacy_admin_users_table_gets_auth_version_column_at_runtime(self):
        self.recreate_legacy_admin_users_table_without_auth_version()

        with SessionLocal() as db:
            db.execute(
                text(
                    """
                    INSERT INTO admin_users (id, username, display_name, password_hash, role, is_enabled)
                    VALUES (:id, :username, :display_name, :password_hash, :role, :is_enabled)
                    """
                ),
                {
                    "id": 1,
                    "username": "legacy-admin",
                    "display_name": "Legacy Admin",
                    "password_hash": hash_password("legacy123"),
                    "role": "admin",
                    "is_enabled": True,
                },
            )
            db.commit()

        with SessionLocal() as db:
            init_db_with_seed(db)

        with SessionLocal() as db:
            columns = {column["name"] for column in inspect(db.bind).get_columns("admin_users")}
            self.assertIn("auth_version", columns)

            legacy_user = db.scalar(select(AdminUser).where(AdminUser.username == "legacy-admin"))
            self.assertIsNotNone(legacy_user)
            self.assertEqual(legacy_user.auth_version, 1)

        login_response = self.client.post(
            "/login",
            data={"username": "legacy-admin", "password": "legacy123"},
            follow_redirects=False,
        )
        self.assertEqual(login_response.status_code, 303)

    def test_auth_seed_rules_for_demo_and_bootstrap_admin(self):
        with self.override_settings(
            app_env="prod",
            auth_seed_demo_users=False,
            auth_bootstrap_admin_username="",
            auth_bootstrap_admin_password="",
            auth_bootstrap_admin_display_name="Bootstrap Admin",
        ):
            with self.build_temp_session() as db:
                seed_auth_users(db)
                self.assertEqual(db.scalars(select(AdminUser)).all(), [])

        with self.override_settings(
            app_env="prod",
            auth_seed_demo_users=False,
            auth_bootstrap_admin_username="root-admin",
            auth_bootstrap_admin_password="S3curePass!",
            auth_bootstrap_admin_display_name="Root Admin",
        ):
            with self.build_temp_session() as db:
                db.add(
                    AdminUser(
                        username="readonly-user",
                        display_name="Readonly User",
                        password_hash="hash",
                        role="readonly",
                        is_enabled=True,
                    )
                )
                db.commit()
                seed_auth_users(db)
                users = db.scalars(select(AdminUser).order_by(AdminUser.username.asc())).all()
                self.assertEqual(len(users), 2)
                self.assertEqual([user.username for user in users], ["readonly-user", "root-admin"])
                self.assertEqual(users[1].role, "admin")

        with self.override_settings(
            app_env="dev",
            auth_seed_demo_users=True,
            auth_bootstrap_admin_username="",
            auth_bootstrap_admin_password="",
        ):
            with self.build_temp_session() as db:
                seed_auth_users(db)
                users = db.scalars(select(AdminUser).order_by(AdminUser.username.asc())).all()
                self.assertEqual([user.username for user in users], ["admin", "ops", "viewer"])


if __name__ == "__main__":
    unittest.main()
