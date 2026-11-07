from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient

TEMP_ROOT = Path(__file__).resolve().parents[1] / ".run" / "test-runtime"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DIR = tempfile.mkdtemp(prefix="workwechat-provider-", dir=TEMP_ROOT)
os.environ["DATA_DIR"] = TEST_DIR
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DIR}/test.db"
os.environ["AUTH_SEED_DEMO_USERS"] = "true"
os.environ["LOG_DIR"] = str(TEMP_ROOT / "logs")

from app.models.entities import Channel, Conversation
from app.services.channel_adapters.registry import get_channel_adapter
from app.services.channel_adapters.workwechat import WorkWechatAdapter


MODULE_PATH = Path(__file__).resolve().parents[1] / "vendors" / "workwechat_provider" / "app.py"
SPEC = importlib.util.spec_from_file_location("workwechat_provider_app", MODULE_PATH)
assert SPEC and SPEC.loader
PROVIDER_APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROVIDER_APP
SPEC.loader.exec_module(PROVIDER_APP)

CLOUD_JOBS_PATH = Path(__file__).resolve().parents[1] / "vendors" / "workwechat_provider" / "cloud_jobs.py"
CLOUD_JOBS_SPEC = importlib.util.spec_from_file_location("workwechat_provider_cloud_jobs", CLOUD_JOBS_PATH)
assert CLOUD_JOBS_SPEC and CLOUD_JOBS_SPEC.loader
CLOUD_JOBS = importlib.util.module_from_spec(CLOUD_JOBS_SPEC)
sys.modules[CLOUD_JOBS_SPEC.name] = CLOUD_JOBS
CLOUD_JOBS_SPEC.loader.exec_module(CLOUD_JOBS)


class WorkWechatAdapterTests(unittest.TestCase):
    def test_registry_returns_independent_workwechat_adapter(self):
        channel = Channel(id=9, bot_id=1, name="企微 Hook", channel_type="workwechat")

        adapter = get_channel_adapter(channel)

        self.assertIsInstance(adapter, WorkWechatAdapter)
        self.assertEqual(adapter.adapter_type, "workwechat")

    def test_send_message_posts_to_workwechat_provider(self):
        channel = Channel(
            id=9,
            bot_id=1,
            name="企微 Hook",
            channel_type="workwechat",
            # workwechat provider 使用独立 8181 端口，避免和主服务、底层 Hook 端口混用。
            api_base="http://127.0.0.1:8181",
            adapter_config_json=json.dumps(
                {"headers": {"X-Provider-Token": "provider-token"}},
                ensure_ascii=False,
            ),
        )
        conversation = Conversation(
            id=18,
            bot_id=1,
            channel_id=9,
            channel_type="workwechat",
            external_id="R:room-001",
            title="测试群",
            chat_type="group",
        )

        class FakeResponse:
            status = 200
            url = "http://127.0.0.1:8181/send-message"

            def read(self):
                return json.dumps({"ok": True, "status": "sent", "external_message_id": "ww-msg-1"}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=0):
            self.assertEqual(req.full_url, "http://127.0.0.1:8181/send-message")
            self.assertEqual(req.headers.get("X-provider-token"), "provider-token")
            body = json.loads(req.data.decode("utf-8"))
            self.assertEqual(body["channel_type"], "workwechat")
            self.assertEqual(body["receiver_id"], "R:room-001")
            self.assertEqual(body["conversation_title"], "测试群")
            self.assertEqual(body["message_type"], "text")
            self.assertEqual(body["content"], "你好，企微")
            return FakeResponse()

        with patch("app.services.channel_adapters.generic.urlrequest.urlopen", side_effect=fake_urlopen):
            result = WorkWechatAdapter().send_message(
                channel,
                conversation,
                {"message_type": "text", "content": "你好，企微"},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["adapter_type"], "workwechat")
        self.assertEqual(result["external_message_id"], "ww-msg-1")

    def test_raw_hook_text_message_normalizes_to_inbound_event(self):
        payload = {
            "client_id": 1,
            "type": 11041,
            "data": {
                "conversation_id": "R:room-001",
                "room_name": "测试群",
                "sender": "788130001",
                "sender_name": "张三",
                "content": "收到一条企微消息",
                "send_time": "1711111111",
                "server_id": "srv-001",
            },
        }
        channel = Channel(id=9, bot_id=1, name="企微 Hook", channel_type="workwechat")

        normalized = WorkWechatAdapter().normalize_inbound_payload(channel, payload, headers={"X-Debug": "1"})

        self.assertEqual(normalized["adapter_type"], "workwechat")
        self.assertEqual(normalized["event_key"], "srv-001")
        self.assertEqual(normalized["conversation"]["external_id"], "R:room-001")
        self.assertEqual(normalized["conversation"]["chat_type"], "group")
        self.assertEqual(normalized["message"]["content"], "收到一条企微消息")
        self.assertEqual(normalized["message"]["sender_name"], "张三")
        self.assertEqual(normalized["message"]["message_type"], "text")
        self.assertEqual(normalized["details"]["hook_type"], 11041)

    def test_raw_hook_message_uses_group_name_as_conversation_title(self):
        payload = {
            "client_id": 1,
            "type": 11041,
            "data": {
                "conversation_id": "R:room-002",
                "group_name": "供应商订单群",
                "sender": "788130002",
                "sender_name": "李四",
                "content": "回传订单",
                "send_time": "1711111112",
                "server_id": "srv-002",
            },
        }
        channel = Channel(id=9, bot_id=1, name="企微 Hook", channel_type="workwechat")

        normalized = WorkWechatAdapter().normalize_inbound_payload(channel, payload, headers={})

        self.assertEqual(normalized["conversation"]["external_id"], "R:room-002")
        self.assertEqual(normalized["conversation"]["title"], "供应商订单群")
        self.assertEqual(normalized["conversation"]["chat_type"], "group")

    def test_raw_hook_self_sent_message_marks_outbound_storage_only(self):
        payload = {
            "client_id": 1,
            "type": 11041,
            "data": {
                "conversation_id": "R:room-003",
                "room_name": "客服群",
                "sender": "self-user",
                "sender_name": "客服",
                "content": "我手动发出的消息",
                "send_time": "1711111113",
                "server_id": "srv-self-001",
                "is_self_sent": True,
                "self_user_id": "self-user",
            },
        }
        channel = Channel(id=9, bot_id=1, name="企微 Hook", channel_type="workwechat")

        normalized = WorkWechatAdapter().normalize_inbound_payload(channel, payload, headers={})

        self.assertEqual(normalized["details"]["direction"], "out")
        self.assertTrue(normalized["details"]["is_self_sent"])
        self.assertTrue(normalized["details"]["skip_plugin_dispatch"])

    def test_provider_file_payload_without_content_is_persistable(self):
        payload = {
            "event_key": "provider-file-001",
            "event_type": "message",
            "conversation": {
                "external_id": "R:room-004",
                "title": "供应商群",
                "chat_type": "group",
            },
            "message": {
                "content": "",
                "sender_id": "supplier-user",
                "sender_name": "供应商",
                "message_type": "file",
                "external_message_id": "provider-file-001",
            },
            "details": {
                "direction": "in",
                "file_name": "回单.xls",
                "download_path": r"D:\\tmp\\回单.xls",
            },
        }
        channel = Channel(id=9, bot_id=1, name="企微 Provider", channel_type="workwechat")

        normalized = WorkWechatAdapter().normalize_inbound_payload(channel, payload, headers={})

        self.assertTrue(normalized["should_persist_message"])
        self.assertEqual(normalized["message"]["content"], "[file] 回单.xls")
        self.assertEqual(normalized["details"]["direction"], "in")


class WorkWechatProviderTests(unittest.TestCase):
    def test_cloud_job_lock_prevents_duplicate_processing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)

            first = CLOUD_JOBS.try_acquire_job_lock(state_dir, job_id=123, payload_hash="hash-a", event_id="evt-1")
            second = CLOUD_JOBS.try_acquire_job_lock(state_dir, job_id=123, payload_hash="hash-a", event_id="evt-1")

        self.assertTrue(first)
        self.assertFalse(second)

    def test_cloud_job_processed_cache_prevents_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            CLOUD_JOBS.mark_job_processed(
                state_dir,
                job_id=123,
                payload_hash="hash-a",
                result={"provider_message_id": "ww-msg-1"},
            )

            self.assertTrue(CLOUD_JOBS.is_job_processed(state_dir, job_id=123, payload_hash="hash-a"))
            self.assertTrue(CLOUD_JOBS.has_processed_payload_conflict(state_dir, job_id=123, payload_hash="hash-b"))

    def test_cloud_file_job_waits_for_self_sent_delivery_confirmation(self):
        completed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        def fake_post_json(url, body, *, timeout):
            self.assertEqual(body["type"], 11031)
            return {"success": True, "message": "消息已发送", "type": 11031}

        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            with (
                patch.object(PROVIDER_APP, "resolve_cloud_jobs_state_dir", return_value=state_dir),
                patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json),
                patch.object(
                    PROVIDER_APP,
                    "report_cloud_job_complete",
                    side_effect=lambda job_id, result: completed.append({"job_id": job_id, "result": result})
                    or {"ok": True, "job_id": job_id, "status": "completed"},
                ),
                patch.object(
                    PROVIDER_APP,
                    "report_cloud_job_fail",
                    side_effect=lambda job_id, code, result: failed.append({"job_id": job_id, "code": code, "result": result})
                    or {"ok": False, "job_id": job_id, "status": "failed"},
                ),
            ):
                result = PROVIDER_APP.process_cloud_job(
                    {
                        "job_id": 9101,
                        "payload_hash": "hash-file-9101",
                        "payload": {
                            "conversation_external_id": "R:room-001",
                            "message_type": "file",
                            "payload": {"file_path": r"D:\tmp\订单.xlsx"},
                        },
                    }
                )

                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "pending_delivery")
                self.assertEqual(completed, [])
                self.assertEqual(failed, [])
                self.assertIn("9101", PROVIDER_APP._load_pending_delivery_records())

                confirmed = PROVIDER_APP.confirm_pending_delivery_from_echo(
                    {
                        "event_key": "srv-file-9101",
                        "conversation": {"external_id": "R:room-001"},
                        "message": {
                            "message_type": "file",
                            "content": "[file] 订单.xlsx",
                            "external_message_id": "srv-file-9101",
                        },
                        "details": {
                            "is_self_sent": True,
                            "direction": "out",
                            "file_name": "订单.xlsx",
                        },
                    }
                )
                remaining_records = PROVIDER_APP._load_pending_delivery_records()

            self.assertTrue(confirmed["matched"])
            self.assertEqual(completed[0]["job_id"], 9101)
            adapter_result = completed[0]["result"]["adapter_result"]
            self.assertEqual(adapter_result["status"], "delivered")
            self.assertEqual(adapter_result["external_status"], "delivered")
            self.assertEqual(adapter_result["external_message_id"], "srv-file-9101")
            self.assertEqual(remaining_records, {})
            self.assertTrue(CLOUD_JOBS.is_job_processed(state_dir, job_id=9101, payload_hash="hash-file-9101"))

    def test_pending_delivery_timeout_reports_cloud_job_failed(self):
        failed: list[dict[str, Any]] = []

        def fake_post_json(url, body, *, timeout):
            self.assertEqual(body["type"], 11031)
            return {"success": True, "message": "消息已发送", "type": 11031}

        original_config = PROVIDER_APP.config
        PROVIDER_APP.config = PROVIDER_APP.ProviderConfig(delivery_confirmation_timeout_seconds=10)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                state_dir = Path(temp_dir)
                with (
                    patch.object(PROVIDER_APP, "resolve_cloud_jobs_state_dir", return_value=state_dir),
                    patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json),
                    patch.object(
                        PROVIDER_APP,
                        "report_cloud_job_fail",
                        side_effect=lambda job_id, code, result: failed.append({"job_id": job_id, "code": code, "result": result})
                        or {"ok": False, "job_id": job_id, "status": "pending"},
                    ),
                ):
                    result = PROVIDER_APP.process_cloud_job(
                        {
                            "job_id": 9102,
                            "payload_hash": "hash-file-9102",
                            "payload": {
                                "conversation_external_id": "R:room-001",
                                "message_type": "file",
                                "payload": {"file_path": r"D:\tmp\订单.xlsx"},
                            },
                        }
                    )
                    self.assertEqual(result["status"], "pending_delivery")

                    records = PROVIDER_APP._load_pending_delivery_records()
                    records["9102"]["expires_at"] = 1
                    PROVIDER_APP._store_pending_delivery_records(records)

                    flushed = PROVIDER_APP.flush_pending_delivery_confirmations()
                    remaining_records = PROVIDER_APP._load_pending_delivery_records()
        finally:
            PROVIDER_APP.config = original_config

        self.assertEqual(flushed["timed_out"], 1)
        self.assertEqual(failed[0]["job_id"], 9102)
        self.assertEqual(failed[0]["code"], "delivery_confirmation_timeout")
        self.assertEqual(failed[0]["result"]["external_status"], "accepted_unconfirmed")
        self.assertEqual(remaining_records, {})

    def test_provider_config_includes_launcher_defaults(self):
        cfg = PROVIDER_APP.ProviderConfig()

        self.assertEqual(cfg.required_workwechat_version, "5.0.0.6008")
        self.assertEqual(cfg.workwechat_process_name, "WXWork.exe")
        self.assertEqual(cfg.hook_runtime_executable, "")
        self.assertFalse(cfg.cloud_poll_enabled)
        self.assertEqual(cfg.cloud_base_url, "")
        self.assertEqual(cfg.authorization_code, "")
        self.assertEqual(cfg.client_token, "")
        self.assertTrue(cfg.delivery_confirmation_enabled)
        self.assertEqual(cfg.delivery_confirmation_timeout_seconds, 180)

    def test_resolve_hook_runtime_executable_prefers_configured_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_exe = Path(temp_dir) / "WorkWechatHookRuntime.exe"
            runtime_exe.write_bytes(b"exe")

            with patch.object(
                PROVIDER_APP,
                "config",
                PROVIDER_APP.ProviderConfig(hook_runtime_executable=str(runtime_exe)),
            ):
                self.assertEqual(PROVIDER_APP.resolve_hook_runtime_executable(), runtime_exe)

    def test_build_hook_runtime_command_uses_packaged_exe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_dir = Path(temp_dir)
            runtime_exe = runtime_dir / "WorkWechatHookRuntime.exe"
            runtime_exe.write_bytes(b"exe")

            with patch.object(
                PROVIDER_APP,
                "config",
                PROVIDER_APP.ProviderConfig(hook_runtime_dir=str(runtime_dir)),
            ):
                command, mode = PROVIDER_APP.build_hook_runtime_command(runtime_dir)

        self.assertEqual(command, [str(runtime_exe)])
        self.assertEqual(mode, "executable")

    def test_register_cloud_client_saves_client_token_without_channel_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "cloud_poll_enabled": True,
                        "cloud_base_url": "https://cloud.example.com",
                        "authorization_code": "wwauth-demo",
                        "provider_name": "通用客户端",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def fake_cloud_post(path, body, token=""):
                self.assertEqual(path, "/api/workwechat/provider/register")
                self.assertNotIn("channel_id", body)
                return {
                    "ok": True,
                    "channel_id": 9,
                    "client_token": "wwclient-demo",
                    "inbound_mode": "client_token",
                    "inbound_url": "https://cloud.example.com/api/workwechat/provider/inbound",
                    "inbound_upload_url": "https://cloud.example.com/api/workwechat/provider/inbound-upload",
                    "callback_url": "",
                    "callback_secret": "",
                    "poll_interval_seconds": 30,
                    "poll_batch_size": 5,
                }

            with patch.object(PROVIDER_APP, "CONFIG_PATH", config_path), patch.object(
                PROVIDER_APP,
                "config",
                PROVIDER_APP.ProviderConfig(
                    cloud_poll_enabled=True,
                    cloud_base_url="https://cloud.example.com",
                    authorization_code="wwauth-demo",
                    provider_name="通用客户端",
                ),
            ), patch.object(PROVIDER_APP, "cloud_post_json", side_effect=fake_cloud_post):
                result = PROVIDER_APP.register_cloud_client_if_needed()

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(saved["client_token"], "wwclient-demo")
            self.assertEqual(saved["cloud_channel_id"], 9)
            self.assertEqual(saved["authorization_code"], "")
            self.assertEqual(saved["callback_url"], "")
            self.assertEqual(saved["callback_secret"], "")

    def test_cloud_push_callback_uses_client_token_inbound_endpoint(self):
        captured: dict[str, Any] = {}

        def fake_cloud_post(path, body, token=""):
            captured.update({"path": path, "body": body, "token": token})
            return {"ok": True, "event_id": 88}

        payload = {"event_key": "evt-cloud-inbound", "message": {"content": "hello"}}
        with patch.object(
            PROVIDER_APP,
            "config",
            PROVIDER_APP.ProviderConfig(
                cloud_poll_enabled=True,
                cloud_base_url="https://cloud.example.com",
                client_token="wwclient-demo",
                callback_url="https://cloud.example.com/webhooks/9/inbound",
                callback_secret="legacy-secret",
            ),
        ), patch.object(PROVIDER_APP, "cloud_post_json", side_effect=fake_cloud_post):
            result = PROVIDER_APP.push_callback(payload)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["path"], "/api/workwechat/provider/inbound")
        self.assertEqual(captured["token"], "wwclient-demo")
        self.assertEqual(captured["body"], payload)

    def test_hook_callback_returns_before_cloud_push_runs(self):
        class FakeHookRequest:
            method = "POST"
            url = "http://127.0.0.1:8181/hook-callback"
            client = SimpleNamespace(host="127.0.0.1")
            headers = {}

            async def json(self):
                return {
                    "type": 11041,
                    "data": {
                        "conversation_id": "R:room-001",
                        "sender": "788130001",
                        "sender_name": "张三",
                        "content": "收到一条企微消息",
                        "send_time": "1711111111",
                        "server_id": "srv-001",
                    },
                }

        with patch.object(PROVIDER_APP, "submit_inbound_callback", return_value={"sequence": 1, "pending": 1}) as submit_callback:
            with patch.object(PROVIDER_APP, "push_callback_background") as background_push:
                result = asyncio.run(PROVIDER_APP.hook_callback(FakeHookRequest()))

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["callback_mode"], "threadpool")
        self.assertEqual(result["callback_queue"], {"sequence": 1, "pending": 1})
        submit_callback.assert_called_once()
        background_push.assert_not_called()

    def test_submit_inbound_callback_updates_queue_status(self):
        class ImmediateExecutor:
            def submit(self, func, *args, **kwargs):
                future = PROVIDER_APP.Future()
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
                return future

        fresh_status = {
            "submitted": 0,
            "completed": 0,
            "failed": 0,
            "pending": 0,
            "last_submitted_at": "",
            "last_completed_at": "",
            "last_failed_at": "",
            "last_error": "",
        }
        payload = {"event_key": "evt-cloud-inbound", "message": {"content": "hello"}}
        with patch.object(PROVIDER_APP, "inbound_callback_status", fresh_status):
            with patch.object(PROVIDER_APP, "inbound_callback_executor", ImmediateExecutor()):
                with patch.object(PROVIDER_APP, "push_callback_background", return_value=None) as background_push:
                    queued = PROVIDER_APP.submit_inbound_callback(payload)

        self.assertEqual(queued["sequence"], 1)
        self.assertEqual(queued["pending"], 1)
        self.assertEqual(fresh_status["submitted"], 1)
        self.assertEqual(fresh_status["completed"], 1)
        self.assertEqual(fresh_status["failed"], 0)
        self.assertEqual(fresh_status["pending"], 0)
        background_push.assert_called_once_with(payload)

    def test_health_returns_cached_hook_status_without_blocking_probe(self):
        class FakeHealthRequest:
            method = "GET"
            url = "http://127.0.0.1:8181/health"
            client = SimpleNamespace(host="127.0.0.1")
            headers = {}

        class DeferredExecutor:
            def __init__(self):
                self.submitted = []

            def submit(self, func, *args, **kwargs):
                self.submitted.append((func, args, kwargs))
                return PROVIDER_APP.Future()

        fresh_status = {
            "refreshing": False,
            "last_started_at": "",
            "last_checked_at": "",
            "last_duration_ms": None,
            "last_error": "",
            "hook_status": {"ok": None, "reason": "probe_pending"},
            "hook_runtime_status": {"ok": None, "reason": "probe_pending"},
            "_last_checked_epoch": 0.0,
        }
        executor = DeferredExecutor()
        original_config = PROVIDER_APP.config
        PROVIDER_APP.config = PROVIDER_APP.ProviderConfig(provider_token="", request_timeout_seconds=12)
        try:
            with patch.object(PROVIDER_APP, "hook_health_status", fresh_status):
                with patch.object(PROVIDER_APP, "hook_health_executor", executor):
                    with patch.object(PROVIDER_APP, "post_json", side_effect=AssertionError("health must not block on hook api")):
                        payload = asyncio.run(PROVIDER_APP.health(FakeHealthRequest()))
        finally:
            PROVIDER_APP.config = original_config

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["hook_status"], {"ok": None, "reason": "probe_pending"})
        self.assertTrue(payload["hook_health_refresh"]["submitted"])
        self.assertEqual(len(executor.submitted), 1)

    def test_hook_health_probe_does_not_submit_duplicate_refresh(self):
        class DeferredExecutor:
            def __init__(self):
                self.submit_count = 0

            def submit(self, func, *args, **kwargs):
                self.submit_count += 1
                return PROVIDER_APP.Future()

        fresh_status = {
            "refreshing": False,
            "last_started_at": "",
            "last_checked_at": "",
            "last_duration_ms": None,
            "last_error": "",
            "hook_status": {"ok": None, "reason": "probe_pending"},
            "hook_runtime_status": {"ok": None, "reason": "probe_pending"},
            "_last_checked_epoch": 0.0,
        }
        executor = DeferredExecutor()
        with patch.object(PROVIDER_APP, "hook_health_status", fresh_status):
            with patch.object(PROVIDER_APP, "hook_health_executor", executor):
                first = PROVIDER_APP.submit_hook_health_probe_if_needed()
                second = PROVIDER_APP.submit_hook_health_probe_if_needed()

        self.assertTrue(first["submitted"])
        self.assertFalse(second["submitted"])
        self.assertEqual(second["reason"], "already_refreshing")
        self.assertEqual(executor.submit_count, 1)

    def test_hook_health_probe_result_is_cached_for_health_response(self):
        class FakeHealthRequest:
            method = "GET"
            url = "http://127.0.0.1:8181/health"
            client = SimpleNamespace(host="127.0.0.1")
            headers = {}

        fresh_status = {
            "refreshing": False,
            "last_started_at": "",
            "last_checked_at": "",
            "last_duration_ms": None,
            "last_error": "",
            "hook_status": {"ok": None, "reason": "probe_pending"},
            "hook_runtime_status": {"ok": None, "reason": "probe_pending"},
            "_last_checked_epoch": 0.0,
        }
        finished = PROVIDER_APP.Future()
        finished.set_result(
            {
                "hook_status": {"ok": True, "response": {"ok": True}},
                "hook_runtime_status": {"ok": True, "pid": 1234},
                "duration_ms": 15,
            }
        )
        original_config = PROVIDER_APP.config
        PROVIDER_APP.config = PROVIDER_APP.ProviderConfig(provider_token="", request_timeout_seconds=12)
        try:
            with patch.object(PROVIDER_APP, "hook_health_status", fresh_status):
                PROVIDER_APP.mark_hook_health_probe_done(finished)
                payload = asyncio.run(PROVIDER_APP.health(FakeHealthRequest()))
        finally:
            PROVIDER_APP.config = original_config

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["hook_status"]["ok"], True)
        self.assertEqual(payload["hook_runtime_status"]["pid"], 1234)
        self.assertEqual(payload["hook_health_cache"]["last_duration_ms"], 15)

    def test_push_callback_background_logs_result(self):
        payload = {"event_key": "evt-cloud-inbound", "message": {"content": "hello"}}
        with patch.object(PROVIDER_APP, "push_callback", return_value={"ok": True, "event_id": 88}) as push:
            with patch.object(PROVIDER_APP, "log_debug_event") as log_debug:
                PROVIDER_APP.push_callback_background(payload)

        push.assert_called_once_with(payload)
        self.assertEqual(log_debug.call_args.args[0], "provider hook-callback background response")
        self.assertTrue(log_debug.call_args.args[1]["ok"])

    def test_cloud_inbound_upload_url_uses_provider_api_endpoint(self):
        with patch.object(
            PROVIDER_APP,
            "config",
            PROVIDER_APP.ProviderConfig(
                cloud_poll_enabled=True,
                cloud_base_url="https://cloud.example.com",
                client_token="wwclient-demo",
                callback_url="https://legacy.example.com/webhooks/9/inbound",
            ),
        ):
            upload_url = PROVIDER_APP.build_main_service_upload_url()

        self.assertEqual(upload_url, "https://cloud.example.com/api/workwechat/provider/inbound-upload")

    def test_process_cloud_job_uses_idempotency_and_reports_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            completed: list[dict[str, Any]] = []

            def fake_hook_post(url, body, timeout=0):
                self.assertEqual(body["type"], 11029)
                return {"ok": True, "message": "sent"}

            def fake_complete(job_id, result):
                completed.append({"job_id": job_id, "result": result})
                return {"ok": True}

            job = {
                "job_id": 123,
                "payload_hash": "hash-a",
                "payload": {"receiver_id": "R:room-001", "message_type": "text", "content": "hello"},
            }

            with (
                patch.object(PROVIDER_APP, "resolve_cloud_jobs_state_dir", return_value=Path(temp_dir)),
                patch.object(PROVIDER_APP, "config", PROVIDER_APP.ProviderConfig(delivery_confirmation_enabled=False)),
                patch.object(
                    PROVIDER_APP,
                    "post_json",
                    side_effect=fake_hook_post,
                ),
                patch.object(PROVIDER_APP, "report_cloud_job_complete", side_effect=fake_complete),
            ):
                result = PROVIDER_APP.process_cloud_job(job)
                replay = PROVIDER_APP.process_cloud_job(job)

            self.assertTrue(result["ok"])
            self.assertEqual(replay["status"], "already_processed")
            self.assertEqual(len(completed), 2)

    def test_handle_cloud_ws_event_job_available_triggers_poll(self):
        with patch.object(PROVIDER_APP, "poll_cloud_jobs_once", return_value={"ok": True, "job_count": 1}) as mocked:
            result = PROVIDER_APP.handle_cloud_ws_event({"event_type": "job_available", "job_id": 123})

        self.assertTrue(result["ok"])
        mocked.assert_called_once()

    def test_handle_cloud_ws_open_polls_pending_jobs(self):
        with patch.object(PROVIDER_APP, "poll_cloud_jobs_once", return_value={"ok": True, "job_count": 2}) as mocked:
            result = PROVIDER_APP.handle_cloud_ws_open()

        self.assertTrue(result["ok"])
        mocked.assert_called_once()
        self.assertTrue(PROVIDER_APP.cloud_status["ws_connected"])
        self.assertTrue(PROVIDER_APP.cloud_status["last_poll_at"])

    def test_cloud_poll_fallback_loop_polls_periodically(self):
        wait_calls: list[int] = []

        def fake_wait(seconds: int):
            wait_calls.append(seconds)
            if len(wait_calls) >= 2:
                PROVIDER_APP.cloud_ws_stop_event.set()
            return False

        PROVIDER_APP.cloud_ws_stop_event.clear()
        try:
            with (
                patch.object(
                    PROVIDER_APP,
                    "config",
                    PROVIDER_APP.ProviderConfig(
                        cloud_poll_enabled=True,
                        cloud_base_url="https://cloud.example.com",
                        client_token="wwclient-demo",
                        poll_interval_seconds=3,
                    ),
                ),
                patch.object(PROVIDER_APP.cloud_ws_stop_event, "wait", side_effect=fake_wait),
                patch.object(PROVIDER_APP, "poll_cloud_jobs_once", return_value={"ok": True, "job_count": 0}) as mocked,
            ):
                PROVIDER_APP.run_cloud_poll_fallback_loop()
        finally:
            PROVIDER_APP.cloud_ws_stop_event.clear()

        self.assertEqual(wait_calls, [3, 3])
        self.assertEqual(mocked.call_count, 2)

    def test_build_cloud_ws_url_uses_token_without_channel_id(self):
        with patch.object(
            PROVIDER_APP,
            "config",
            PROVIDER_APP.ProviderConfig(cloud_base_url="https://cloud.example.com", client_token="wwclient-demo"),
        ):
            url = PROVIDER_APP.build_cloud_ws_url()

        self.assertEqual(url, "wss://cloud.example.com/api/workwechat/provider/ws?client_token=wwclient-demo")

    def test_cloud_ws_loop_backs_off_after_normal_disconnect(self):
        # WebSocket 正常断开时 run_forever 会直接返回；循环必须先退避，
        # 否则云端反复断开会触发 provider 高速重连并持续占用 CPU。
        sleep_calls: list[int] = []
        run_calls = 0

        def fake_run_cloud_ws_once():
            nonlocal run_calls
            run_calls += 1
            if run_calls > 1:
                raise RuntimeError("reconnected_without_backoff")

        def fake_sleep(seconds: int):
            sleep_calls.append(seconds)
            PROVIDER_APP.cloud_ws_stop_event.set()

        PROVIDER_APP.cloud_ws_stop_event.clear()
        try:
            with (
                patch.object(
                    PROVIDER_APP,
                    "config",
                    PROVIDER_APP.ProviderConfig(
                        cloud_poll_enabled=True,
                        cloud_base_url="https://cloud.example.com",
                        client_token="wwclient-demo",
                        poll_interval_seconds=3,
                    ),
                ),
                patch.object(PROVIDER_APP, "register_cloud_client_if_needed", return_value={"ok": True}),
                patch.object(PROVIDER_APP, "_run_cloud_ws_once", side_effect=fake_run_cloud_ws_once),
                patch.object(PROVIDER_APP, "_sleep_with_stop", side_effect=fake_sleep),
                patch.object(PROVIDER_APP, "poll_cloud_jobs_once", return_value={"ok": True}),
            ):
                PROVIDER_APP.run_cloud_ws_loop()
        finally:
            PROVIDER_APP.cloud_ws_stop_event.clear()

        self.assertEqual(run_calls, 1)
        self.assertEqual(sleep_calls, [3])

    def test_build_hook_command_maps_text_image_and_file(self):
        text_command = PROVIDER_APP.build_hook_command(
            {"receiver_id": "R:room-001", "message_type": "text", "content": "hello"}
        )
        image_command = PROVIDER_APP.build_hook_command(
            {"receiver_id": "R:room-001", "message_type": "image", "payload": {"image_path": "D:\\tmp\\a.png"}}
        )
        file_command = PROVIDER_APP.build_hook_command(
            {"receiver_id": "R:room-001", "message_type": "file", "payload": {"file_path": "D:\\tmp\\a.xlsx"}}
        )

        self.assertEqual(text_command["type"], 11029)
        self.assertEqual(text_command["data"]["content"], "hello")
        self.assertEqual(image_command["type"], 11030)
        self.assertEqual(image_command["data"]["file_path"], "D:\\tmp\\a.png")
        self.assertEqual(file_command["type"], 11031)
        # 企微 Hook 文档中 11031 文件消息使用 file 字段；file_path 会被 Hook 接口表面接受但实际不发文件。
        self.assertEqual(file_command["data"]["file"], "D:\\tmp\\a.xlsx")
        self.assertNotIn("file_path", file_command["data"])

    def test_build_hook_command_maps_link_card_and_mini_program(self):
        card_command = PROVIDER_APP.build_hook_command(
            {
                "receiver_id": "S:staff_customer",
                "message_type": "link_card",
                "url": "https://example.com/order/1",
                "image_url": "https://example.com/card.jpg",
                "title": "订单详情",
                "desc": "查看订单处理进度",
            }
        )
        mini_command = PROVIDER_APP.build_hook_command(
            {
                "receiver_id": "S:staff_customer",
                "message_type": "mini_program",
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
            }
        )

        self.assertEqual(card_command["type"], 11159)
        self.assertEqual(card_command["data"]["url"], "https://example.com/order/1")
        self.assertEqual(card_command["data"]["title"], "订单详情")
        self.assertEqual(mini_command["type"], 11162)
        self.assertEqual(mini_command["data"]["appid"], "wx-demo")
        self.assertEqual(mini_command["data"]["page_path"], "pages/order/detail?id=1")

    def test_build_hook_command_downloads_file_url_before_file_send(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            downloaded_path = Path(temp_dir) / "downloaded.xlsx"

            class FakeResponse:
                status = 200
                url = "http://main.test/outbound-files/1/demo/%E8%AE%A2%E5%8D%95.xlsx"

                def read(self):
                    return b"xlsx-bytes"

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            def fake_urlopen(req, timeout=0):
                self.assertEqual(req.full_url, "http://main.test/outbound-files/1/demo/%E8%AE%A2%E5%8D%95.xlsx")
                return FakeResponse()

            with patch.object(PROVIDER_APP, "resolve_outbound_file_cache_dir", return_value=Path(temp_dir)):
                with patch.object(PROVIDER_APP.urlrequest, "urlopen", side_effect=fake_urlopen):
                    command = PROVIDER_APP.build_hook_command(
                        {
                            "receiver_id": "R:room-001",
                            "message_type": "file",
                            "payload": {
                                "file_name": "订单.xlsx",
                                "file_url": "http://main.test/outbound-files/1/demo/%E8%AE%A2%E5%8D%95.xlsx",
                            },
                        }
                    )

            # URL 下载后的本地路径同样必须放入 file 字段，避免云端任务被误标记 sent。
            resolved_path = Path(command["data"]["file"])
            downloaded_path = resolved_path
            self.assertEqual(command["type"], 11031)
            self.assertNotIn("file_path", command["data"])
            self.assertTrue(downloaded_path.exists())
            self.assertEqual(downloaded_path.read_bytes(), b"xlsx-bytes")
            # 出站缓存用唯一子目录隔离重名文件，文件本身保持原始名称，避免企微附件名带缓存前缀。
            self.assertEqual(downloaded_path.name, "订单.xlsx")
            self.assertNotEqual(downloaded_path.parent, Path(temp_dir))

    def test_upload_inbound_file_to_main_service_encodes_and_pushes_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local_file = Path(temp_dir) / "导入赔付单20260604-1707.xls"
            local_file.write_bytes(b"inbound-file-bytes")

            captured: dict[str, object] = {}

            def fake_post_json(url, body, *, timeout, headers=None):
                captured["url"] = url
                captured["body"] = body
                captured["timeout"] = timeout
                captured["headers"] = headers or {}
                return {
                    "ok": True,
                    "storage_path": r"D:\\learn\\multi-bot-admin\\data\\inbound_files\\2\\share\\导入赔付单20260604-1707.xls",
                    "download_url": "http://main.test/inbound-files/2/share/%E5%AF%BC%E5%85%A5%E8%B5%94%E4%BB%98%E5%8D%9520260604-1707.xls",
                    "file_size": len(b"inbound-file-bytes"),
                    "content_type": "application/octet-stream",
                }

            with patch.object(PROVIDER_APP, "build_main_service_upload_url", return_value="http://main.test/webhooks/2/inbound-upload"):
                with patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json):
                    result = PROVIDER_APP.upload_inbound_file_to_main_service(
                        str(local_file),
                        file_name="导入赔付单20260604-1707.xls",
                        message_type="file",
                        source_event_key="1154891",
                    )

            self.assertEqual(captured["url"], "http://main.test/webhooks/2/inbound-upload")
            self.assertEqual(captured["headers"], {"X-Provider-Token": "replace-with-provider-token"})
            self.assertEqual(captured["body"]["message_type"], "file")
            self.assertEqual(captured["body"]["file_name"], "导入赔付单20260604-1707.xls")
            self.assertEqual(captured["body"]["source_event_key"], "1154891")
            self.assertEqual(result["download_path"], r"D:\\learn\\multi-bot-admin\\data\\inbound_files\\2\\share\\导入赔付单20260604-1707.xls")
            self.assertEqual(result["file_size"], len(b"inbound-file-bytes"))
            self.assertEqual(result["download_url"], "http://main.test/inbound-files/2/share/%E5%AF%BC%E5%85%A5%E8%B5%94%E4%BB%98%E5%8D%9520260604-1707.xls")
            self.assertEqual(result["mime_type"], "application/octet-stream")

    def test_download_inbound_hook_media_uses_top_level_cdn_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_path = Path(temp_dir) / "新建 XLS 工作表.xls"
            captured: dict[str, object] = {}

            def fake_post_json(url, body, *, timeout):
                captured["url"] = url
                captured["body"] = body
                captured["timeout"] = timeout
                save_path.write_bytes(b"xls-bytes")
                return {"ok": True, "data": {"save_path": str(save_path)}}

            def fake_upload(source_path, *, file_name, message_type, source_event_key=""):
                return {
                    "download_path": source_path,
                    "file_name": file_name,
                    "message_type": message_type,
                    "source_event_key": source_event_key,
                }

            with patch.object(PROVIDER_APP, "build_inbound_cache_file_path", return_value=str(save_path)):
                with patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json):
                    with patch.object(PROVIDER_APP, "upload_inbound_file_to_main_service", side_effect=fake_upload):
                        result = PROVIDER_APP.download_inbound_hook_media_if_needed(
                            {
                                "cdn": {
                                    "aes_key": "aes-key",
                                    "auth_key": "auth-key",
                                    "file_name": "新建 XLS 工作表.xls",
                                    "size": 20992,
                                    "url": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=xxx",
                                },
                                "cdn_type": 1,
                                "server_id": "1154935",
                            },
                            message_type="file",
                        )

            command = captured["body"]
            self.assertEqual(command["type"], 11171)
            self.assertEqual(command["data"]["url"], "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=xxx")
            self.assertEqual(command["data"]["auth_key"], "auth-key")
            self.assertEqual(command["data"]["aes_key"], "aes-key")
            self.assertEqual(command["data"]["size"], 20992)
            self.assertEqual(command["data"]["save_path"], str(save_path))
            self.assertEqual(result["download_path"], str(save_path))
            self.assertEqual(result["file_name"], "新建 XLS 工作表.xls")
            self.assertEqual(result["source_event_key"], "1154935")

    def test_download_inbound_hook_media_handles_c2c_cdn_delayed_file_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_path = Path(temp_dir) / "机器人返单.xlsx"
            captured: dict[str, object] = {}
            original_config = PROVIDER_APP.config
            PROVIDER_APP.config = PROVIDER_APP.ProviderConfig(
                request_timeout_seconds=12,
                inbound_file_download_timeout_seconds=45,
                provider_token="replace-with-provider-token",
                callback_url="http://main.test/webhooks/2/inbound",
            )

            def fake_post_json(url, body, *, timeout):
                captured["url"] = url
                captured["body"] = body
                captured["timeout"] = timeout
                # 模拟 Hook Runtime 先返回下载响应，文件随后才真正写入缓存目录。
                return {"ok": True, "data": {"save_path": str(save_path)}}

            def fake_sleep(_seconds):
                save_path.write_bytes(b"xlsx-bytes")

            def fake_upload(source_path, *, file_name, message_type, source_event_key=""):
                return {
                    "download_path": source_path,
                    "file_name": file_name,
                    "message_type": message_type,
                    "source_event_key": source_event_key,
                }

            try:
                with patch.object(PROVIDER_APP, "build_inbound_cache_file_path", return_value=str(save_path)):
                    with patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json):
                        with patch.object(PROVIDER_APP.time, "sleep", side_effect=fake_sleep):
                            with patch.object(PROVIDER_APP, "upload_inbound_file_to_main_service", side_effect=fake_upload):
                                result = PROVIDER_APP.download_inbound_hook_media_if_needed(
                                    {
                                        "cdn": {
                                            "aes_key": "aes-key",
                                            "file_id": "file-id",
                                            "file_name": "机器人返单.xlsx",
                                            "size": 4601,
                                        },
                                        "cdn_type": 2,
                                        "server_id": "1001723",
                                    },
                                    message_type="file",
                                )
            finally:
                PROVIDER_APP.config = original_config

            command = captured["body"]
            self.assertEqual(command["type"], 11170)
            self.assertEqual(command["data"]["file_id"], "file-id")
            self.assertEqual(command["data"]["aes_key"], "aes-key")
            self.assertEqual(command["data"]["file_size"], 4601)
            self.assertEqual(command["data"]["file_type"], 5)
            self.assertEqual(command["data"]["timeout_seconds"], 45)
            self.assertEqual(command["data"]["save_path"], str(save_path))
            self.assertEqual(captured["timeout"], 45)
            self.assertEqual(result["download_path"], str(save_path))
            self.assertEqual(result["file_name"], "机器人返单.xlsx")
            self.assertEqual(result["source_event_key"], "1001723")

    def test_download_inbound_hook_media_keeps_explicit_c2c_file_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_path = Path(temp_dir) / "图片.png"
            captured: dict[str, object] = {}

            def fake_post_json(url, body, *, timeout):
                captured["body"] = body
                save_path.write_bytes(b"image-bytes")
                return {"ok": True, "data": {"save_path": str(save_path)}}

            def fake_upload(source_path, *, file_name, message_type, source_event_key=""):
                return {"download_path": source_path, "file_name": file_name}

            with patch.object(PROVIDER_APP, "build_inbound_cache_file_path", return_value=str(save_path)):
                with patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json):
                    with patch.object(PROVIDER_APP, "upload_inbound_file_to_main_service", side_effect=fake_upload):
                        PROVIDER_APP.download_inbound_hook_media_if_needed(
                            {
                                "cdn": {
                                    "aes_key": "aes-key",
                                    "file_id": "file-id",
                                    "file_name": "图片.png",
                                    "file_type": 1,
                                    "size": 1024,
                                },
                                "cdn_type": 2,
                                "server_id": "1002001",
                            },
                            message_type="file",
                        )

            # 现场有些 C2cCDN 消息会明确给出 file_type；这种情况下必须尊重 Hook 原始类型，
            # 只在缺失时才根据消息类型/文件名兜底推断。
            self.assertEqual(captured["body"]["data"]["file_type"], 1)

    def test_download_inbound_hook_media_retries_timeout_with_file_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            save_path = Path(temp_dir) / "回单.xls"
            attempts: list[int] = []
            original_config = PROVIDER_APP.config
            PROVIDER_APP.config = PROVIDER_APP.ProviderConfig(
                request_timeout_seconds=12,
                inbound_file_download_timeout_seconds=45,
                inbound_file_download_retries=1,
                provider_token="replace-with-provider-token",
                callback_url="http://main.test/webhooks/2/inbound",
            )

            def fake_post_json(url, body, *, timeout):
                attempts.append(timeout)
                self.assertEqual(body["data"]["timeout_seconds"], 45)
                if len(attempts) == 1:
                    return {"ok": False, "error": "timeout"}
                save_path.write_bytes(b"xls-bytes")
                return {"ok": True, "data": {"save_path": str(save_path)}}

            def fake_upload(source_path, *, file_name, message_type, source_event_key=""):
                return {
                    "download_path": source_path,
                    "file_name": file_name,
                    "message_type": message_type,
                    "source_event_key": source_event_key,
                }

            try:
                with patch.object(PROVIDER_APP, "build_inbound_cache_file_path", return_value=str(save_path)):
                    with patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json):
                        with patch.object(PROVIDER_APP, "upload_inbound_file_to_main_service", side_effect=fake_upload):
                            result = PROVIDER_APP.download_inbound_hook_media_if_needed(
                                {
                                    "cdn": {
                                        "aes_key": "aes-key",
                                        "auth_key": "auth-key",
                                        "file_name": "回单.xls",
                                        "size": 21504,
                                        "url": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=xxx",
                                    },
                                    "cdn_type": 1,
                                    "server_id": "1154936",
                                },
                                message_type="file",
                            )
            finally:
                PROVIDER_APP.config = original_config

            self.assertEqual(attempts, [45, 45])
            self.assertEqual(result["download_path"], str(save_path))

    def test_inbound_media_download_endpoint_uses_provider_auth(self):
        original_config = PROVIDER_APP.config
        PROVIDER_APP.config = PROVIDER_APP.ProviderConfig(provider_token="provider-token")
        try:
            client = TestClient(PROVIDER_APP.app)
            with patch.object(
                PROVIDER_APP,
                "download_inbound_hook_media_if_needed",
                return_value={
                    "download_path": r"D:\\learn\\multi-bot-admin\\data\\inbound_files\\2\\share\\回单.xlsx",
                    "file_size": 4926,
                },
            ) as download_mock:
                response = client.post(
                    "/inbound-media/download",
                    headers={"X-Provider-Token": "provider-token"},
                    json={
                        "message_type": "file",
                        "data": {
                            "cdn_type": 2,
                            "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926},
                        },
                    },
                )
        finally:
            PROVIDER_APP.config = original_config

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "downloaded")
        download_mock.assert_called_once()
        self.assertEqual(download_mock.call_args.kwargs["message_type"], "file")

    def test_cloud_control_download_inbound_media_reports_complete(self):
        original_config = PROVIDER_APP.config
        PROVIDER_APP.config = PROVIDER_APP.ProviderConfig(client_token="client-token")
        # provider 的云端任务处理会写本地幂等缓存；测试前清理状态目录，避免复跑时命中旧缓存而跳过动作分支。
        shutil.rmtree(PROVIDER_APP.resolve_cloud_jobs_state_dir(), ignore_errors=True)
        try:
            with patch.object(
                PROVIDER_APP,
                "download_inbound_hook_media_if_needed",
                return_value={"download_path": "D:\\cache\\回单.xlsx", "file_size": 4926},
            ) as download_mock:
                with patch.object(PROVIDER_APP, "report_cloud_job_complete", return_value={"ok": True}) as complete_mock:
                    with patch.object(PROVIDER_APP, "report_cloud_job_fail", return_value={"ok": False}) as fail_mock:
                        result = PROVIDER_APP.process_cloud_job(
                            {
                                "job_id": 1991,
                                "job_type": "workwechat_cloud_control",
                                "channel_id": 2,
                                "action": "download_inbound_media",
                                "payload_hash": "hash-download-inbound-media-1991",
                                "payload": {
                                    "attachment_id": 105,
                                    "message_type": "file",
                                    "data": {
                                        "cdn_type": 2,
                                        "cdn": {"file_id": "file-id", "aes_key": "aes-key", "size": 4926},
                                    },
                                },
                            }
                        )
        finally:
            PROVIDER_APP.config = original_config

        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "download_inbound_media")
        self.assertEqual(result["attachment_id"], 105)
        download_mock.assert_called_once()
        complete_mock.assert_called_once()
        fail_mock.assert_not_called()

    def test_provider_normalizes_hook_payload_for_main_webhook(self):
        with patch.object(
            PROVIDER_APP,
            "download_inbound_hook_media_if_needed",
            return_value={
                "download_path": r"D:\\learn\\multi-bot-admin\\data\\inbound_files\\2\\share\\导入赔付单20260604-1707.xls",
                "file_path": r"D:\\learn\\multi-bot-admin\\data\\inbound_files\\2\\share\\导入赔付单20260604-1707.xls",
                "download_url": "http://main.test/inbound-files/2/share/%E5%AF%BC%E5%85%A5%E8%B5%94%E4%BB%98%E5%8D%9520260604-1707.xls",
                "source_path": r"D:\\workwechat\\downloads\\导入赔付单20260604-1707.xls",
            },
        ):
            normalized = PROVIDER_APP.normalize_hook_payload(
                {
                    "type": 11045,
                    "data": {
                        "conversation_id": "R:room-001",
                        "room_name": "测试群",
                        "sender": "788130001",
                        "sender_name": "张三",
                        "cdn": {
                            "file_name": "导入赔付单20260604-1707.xls",
                            "size": 44544,
                            "url": "https://imunion.weixin.qq.com/cgi-bin/mmae-bin/tpdownloadmedia?param=xxx",
                            "auth_key": "auth-key",
                            "aes_key": "aes-key",
                            "cdn_type": 1,
                        },
                        "send_time": "1711111111",
                        "server_id": "srv-file-001",
                    },
                }
            )

        self.assertEqual(normalized["event_key"], "srv-file-001")
        self.assertEqual(normalized["conversation"]["chat_type"], "group")
        self.assertEqual(normalized["message"]["message_type"], "file")
        self.assertEqual(normalized["message"]["content"], "[file] 导入赔付单20260604-1707.xls")
        self.assertEqual(normalized["details"]["hook_type"], 11045)
        self.assertEqual(normalized["details"]["file_name"], "导入赔付单20260604-1707.xls")
        self.assertEqual(normalized["details"]["download_path"], r"D:\\learn\\multi-bot-admin\\data\\inbound_files\\2\\share\\导入赔付单20260604-1707.xls")

    def test_provider_normalizes_contact_deleted_hook_event(self):
        normalized = PROVIDER_APP.normalize_hook_payload(
            {
                "type": 11077,
                "data": {
                    "user_id": "788130-delete",
                    "username": "被删客户",
                    "corp_id": "1970325",
                    "avatar": "https://img.example/avatar.jpg",
                },
            }
        )

        self.assertEqual(normalized["event_type"], "contact_deleted")
        self.assertEqual(normalized["message"]["message_type"], "contact_deleted")
        self.assertEqual(normalized["details"]["hook_type"], 11077)
        self.assertEqual(normalized["details"]["raw_data"]["user_id"], "788130-delete")

    def test_provider_fetches_paginated_hook_chatrooms(self):
        requests = []

        def fake_post_json(url, body, *, timeout):
            requests.append(body)
            page_num = body["data"]["page_num"]
            if page_num == 1:
                return {
                    "ok": True,
                    "data": {
                        "page_num": 1,
                        "page_size": 1,
                        "total": 2,
                        "total_page": 2,
                        "room_list": [{"conversation_id": "R:1", "nickname": "群1"}],
                    },
                }
            return {
                "ok": True,
                "data": {
                    "page_num": 2,
                    "page_size": 1,
                    "total": 2,
                    "total_page": 2,
                    "room_list": [{"conversation_id": "R:2", "nickname": "群2"}],
                },
            }

        with patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json):
            result = PROVIDER_APP.fetch_hook_pages(hook_type=11038, request_data={}, list_key="room_list", page_size=1, max_pages=5)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(requests[0]["type"], 11038)
        self.assertEqual(requests[1]["data"]["page_num"], 2)

    def test_provider_processes_cloud_chatroom_sync_control_job(self):
        requests = []
        completed = []

        def fake_post_json(url, body, *, timeout):
            requests.append(body)
            self.assertEqual(body["type"], 11038)
            return {
                "ok": True,
                "data": {
                    "page_num": 1,
                    "page_size": 100,
                    "total": 1,
                    "total_page": 1,
                    "room_list": [{"conversation_id": "R:cloud-room", "nickname": "云端群"}],
                },
            }

        def fake_complete(job_id, result):
            completed.append({"job_id": job_id, "result": result})
            return {"ok": True}

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(PROVIDER_APP, "resolve_cloud_jobs_state_dir", return_value=Path(temp_dir)),
                patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json),
                patch.object(PROVIDER_APP, "report_cloud_job_complete", side_effect=fake_complete),
            ):
                result = PROVIDER_APP.process_cloud_job(
                    {
                        "job_id": 8801,
                        "action": "sync_chatrooms",
                        "payload": {"page_size": 100, "max_pages": 2},
                        "payload_hash": "hash-cloud-chatrooms",
                    }
                )

        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(completed[0]["job_id"], 8801)
        self.assertEqual(completed[0]["result"]["adapter_result"]["rooms"][0]["conversation_id"], "R:cloud-room")

    def test_provider_processes_cloud_chatroom_member_sync_control_job(self):
        requests = []
        completed = []

        def fake_post_json(url, body, *, timeout):
            requests.append(body)
            self.assertEqual(body["type"], 11040)
            self.assertEqual(body["data"]["conversation_id"], "R:cloud-room")
            return {
                "ok": True,
                "data": {
                    "page_num": 1,
                    "page_size": 100,
                    "total": 1,
                    "total_page": 1,
                    "member_list": [{"user_id": "member-001", "username": "甲"}],
                },
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(PROVIDER_APP, "resolve_cloud_jobs_state_dir", return_value=Path(temp_dir)),
                patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json),
                patch.object(PROVIDER_APP, "report_cloud_job_complete", side_effect=lambda job_id, result: completed.append({"job_id": job_id, "result": result}) or {"ok": True}),
            ):
                result = PROVIDER_APP.process_cloud_job(
                    {
                        "job_id": 8802,
                        "action": "sync_chatroom_members",
                        "payload": {"conversation_id": "R:cloud-room", "page_size": 100, "max_pages": 2},
                        "payload_hash": "hash-cloud-members",
                    }
                )

        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(completed[0]["result"]["adapter_result"]["members"][0]["user_id"], "member-001")

    def test_provider_processes_cloud_contacts_sync_control_job(self):
        requests = []
        completed = []

        def fake_post_json(url, body, *, timeout):
            requests.append(body)
            hook_type = body["type"]
            if hook_type == 11036:
                return {
                    "ok": True,
                    "data": {
                        "page_num": 1,
                        "page_size": 100,
                        "total": 1,
                        "total_page": 1,
                        "user_list": [{"userid": "staff-001", "name": "内部成员"}],
                    },
                }
            if hook_type == 11037:
                return {
                    "ok": True,
                    "data": {
                        "page_num": 1,
                        "page_size": 100,
                        "total": 1,
                        "total_page": 1,
                        "user_list": [{"external_userid": "external-001", "name": "外部联系人"}],
                    },
                }
            return {"ok": False, "error": "unexpected_hook_type"}

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(PROVIDER_APP, "resolve_cloud_jobs_state_dir", return_value=Path(temp_dir)),
                patch.object(PROVIDER_APP, "post_json", side_effect=fake_post_json),
                patch.object(PROVIDER_APP, "report_cloud_job_complete", side_effect=lambda job_id, result: completed.append({"job_id": job_id, "result": result}) or {"ok": True}),
            ):
                result = PROVIDER_APP.process_cloud_job(
                    {
                        "job_id": 8803,
                        "action": "sync_contacts",
                        "payload": {"contact_kind": "all", "page_size": 100, "max_pages": 2},
                        "payload_hash": "hash-cloud-contacts",
                    }
                )

        self.assertTrue(result["ok"])
        self.assertEqual([item["type"] for item in requests], [11036, 11037])
        contacts = completed[0]["result"]["adapter_result"]["contacts"]
        self.assertEqual([item["contact_kind"] for item in contacts], ["internal_member", "external_contact"])


if __name__ == "__main__":
    unittest.main()
