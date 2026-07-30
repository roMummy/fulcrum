from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_vendor_module(module_name: str, filename: str):
    vendor_dir = Path(__file__).resolve().parents[1] / "vendors" / "workwechat_provider"
    module_path = vendor_dir / filename
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(vendor_dir))
    spec.loader.exec_module(module)
    return module


LAUNCHER_CHECKS = load_vendor_module("workwechat_provider_launcher_checks", "launcher_checks.py")
LAUNCHER_STATE = load_vendor_module("workwechat_provider_launcher_state", "launcher_state.py")
LAUNCHER_CONTROLLER = load_vendor_module("workwechat_provider_launcher_controller", "launcher_controller.py")
LAUNCHER_APP = load_vendor_module("workwechat_provider_launcher_app", "launcher_app.py")
HOOK_PROCESS_MATCH = load_vendor_module("workwechat_provider_hook_process_match", "hook_runtime/process_match.py")
HOOK_RETRY_GUARD = load_vendor_module("workwechat_provider_hook_retry_guard", "hook_runtime/retry_guard.py")


class WorkWechatProviderLauncherTests(unittest.TestCase):
    def test_normalize_version_keeps_four_part_version(self):
        self.assertEqual(LAUNCHER_CHECKS.normalize_version("5.0.0.6008"), "5.0.0.6008")

    def test_compare_required_version_requires_exact_match(self):
        self.assertTrue(LAUNCHER_CHECKS.is_required_version("5.0.0.6008", "5.0.0.6008"))
        self.assertFalse(LAUNCHER_CHECKS.is_required_version("5.0.0.6009", "5.0.0.6008"))

    def test_find_workwechat_process_returns_empty_when_missing(self):
        with patch.object(LAUNCHER_CHECKS.psutil, "process_iter", return_value=[]):
            result = LAUNCHER_CHECKS.find_workwechat_process("WXWork.exe")

        self.assertIsNone(result)

    def test_find_workwechat_process_does_not_require_exe_path(self):
        class FakeProcess:
            info = {"pid": 123, "name": "WXWork.exe", "exe": None}

        with patch.object(LAUNCHER_CHECKS.psutil, "process_iter", return_value=[FakeProcess()]):
            result = LAUNCHER_CHECKS.find_workwechat_process("WXWork.exe")

        self.assertEqual(result, {"pid": 123, "name": "WXWork.exe", "exe": ""})

    def test_launcher_status_builds_customer_facing_message(self):
        status = LAUNCHER_STATE.LauncherStatus(
            workwechat_state="version_mismatch",
            current_version="5.0.0.6009",
            required_version="5.0.0.6008",
        )

        self.assertIn("版本", status.summary)
        self.assertIn("5.0.0.6009", status.detail)

    def test_provider_command_prefers_packaged_server_exe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = root / "WorkWechatProviderServer.exe"
            server.write_bytes(b"exe")

            command = LAUNCHER_CONTROLLER.build_provider_command(root, None)

        self.assertEqual(command, [str(server)])

    def test_provider_command_uses_bundled_server_for_onefile_launcher(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "external"
            root.mkdir()
            bundled_root = Path(temp_dir) / "bundled"
            bundled_root.mkdir()
            server = bundled_root / "WorkWechatProviderServer.exe"
            server.write_bytes(b"exe")

            # 单文件 UI 启动器会先把内置 server 复制到稳定资源目录，再从稳定目录启动。
            command = LAUNCHER_CONTROLLER.build_provider_command(root, bundled_root)

        self.assertEqual(command, [str(server)])

    def test_project_workwechat_process_detection_matches_packaged_server(self):
        owner = LAUNCHER_CONTROLLER.PortOwner(
            port=8181,
            pid=123,
            name="WorkWechatProviderServer.exe",
            exe="C:/Users/demo/AppData/Local/WorkWechatProvider/runtime/WorkWechatProviderServer.exe",
            command_line="",
        )

        self.assertTrue(LAUNCHER_CONTROLLER.is_project_workwechat_process(owner))

    def test_project_workwechat_process_detection_matches_source_hook(self):
        owner = LAUNCHER_CONTROLLER.PortOwner(
            port=8989,
            pid=456,
            name="python.exe",
            exe="C:/Python310-32/python.exe",
            command_line="python D:/learn/multi-bot-admin/vendors/workwechat_provider/hook_runtime/http_py.py",
        )

        self.assertTrue(LAUNCHER_CONTROLLER.is_project_workwechat_process(owner))

    def test_project_workwechat_process_detection_rejects_unrelated_process(self):
        owner = LAUNCHER_CONTROLLER.PortOwner(
            port=8181,
            pid=789,
            name="node.exe",
            exe="C:/Program Files/nodejs/node.exe",
            command_line="node server.js",
        )

        self.assertFalse(LAUNCHER_CONTROLLER.is_project_workwechat_process(owner))

    def test_hook_runtime_window_detection_rejects_provider_launcher_title(self):
        # Hook runtime 不能因为窗口标题包含“企业微信”就注入启动器自身；
        # 否则 8181 会在线但 health 一直等待 Hook 返回，UI 显示异常。
        matched = HOOK_PROCESS_MATCH.is_workwechat_window_candidate(
            "C:/Users/demo/Desktop/WorkWechatProvider.exe",
            "企业微信 Provider",
            "WXWork.exe",
        )

        self.assertFalse(matched)

    def test_hook_runtime_window_detection_accepts_wxwork_process(self):
        matched = HOOK_PROCESS_MATCH.is_workwechat_window_candidate(
            "D:/Program Files (x86)/WXWork/WXWork.exe",
            "供应商群",
            "WXWork.exe",
        )

        self.assertTrue(matched)

    def test_hook_runtime_window_detection_accepts_wxwork_with_blank_title(self):
        # 企业微信主窗口标题可能短时间为空，只要进程路径明确是 WXWork.exe，仍可作为 PID 注入候选。
        matched = HOOK_PROCESS_MATCH.is_workwechat_window_candidate(
            "D:/Program Files (x86)/WXWork/WXWork.exe",
            "",
            "WXWork.exe",
        )

        self.assertTrue(matched)

    def test_hook_runtime_window_priority_prefers_exact_title_then_non_empty_title(self):
        candidates = [
            {"pid": 3, "title": ""},
            {"pid": 2, "title": "客户群"},
            {"pid": 1, "title": "企业微信"},
        ]

        candidates.sort(key=HOOK_PROCESS_MATCH.workwechat_window_priority)

        self.assertEqual([candidate["pid"] for candidate in candidates], [1, 2, 3])

    def test_hook_runtime_window_detection_allows_exact_title_when_path_unreadable(self):
        # 普通权限偶发读不到窗口进程路径时，只接受精确主窗口标题，不再接受“企业微信 Provider”这类包含匹配。
        self.assertTrue(HOOK_PROCESS_MATCH.is_workwechat_window_candidate("", "企业微信", "WXWork.exe"))
        self.assertFalse(HOOK_PROCESS_MATCH.is_workwechat_window_candidate("", "企业微信 Provider", "WXWork.exe"))

    def test_hook_runtime_injection_retry_cooldown_clamps_invalid_values(self):
        self.assertEqual(HOOK_RETRY_GUARD.parse_injection_retry_cooldown("bad"), 30.0)
        self.assertEqual(HOOK_RETRY_GUARD.parse_injection_retry_cooldown("1"), 5.0)
        self.assertEqual(HOOK_RETRY_GUARD.parse_injection_retry_cooldown("999"), 300.0)

    def test_hook_runtime_injection_retry_remaining_seconds(self):
        remaining = HOOK_RETRY_GUARD.injection_retry_delay_remaining(
            last_failed_at=100.0,
            now=112.5,
            cooldown_seconds=30.0,
        )

        self.assertEqual(remaining, 17.5)

    def test_cleanup_project_port_owners_terminates_only_project_processes(self):
        project_owner = LAUNCHER_CONTROLLER.PortOwner(
            port=8181,
            pid=123,
            name="WorkWechatProviderServer.exe",
            exe="C:/WorkWechatProviderServer.exe",
            command_line="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(LAUNCHER_CONTROLLER, "iter_listening_port_owners", return_value=[project_owner]):
                with patch.object(LAUNCHER_CONTROLLER, "terminate_process_tree_by_pid") as terminate:
                    with patch.object(LAUNCHER_CONTROLLER, "wait_for_ports_released", return_value=[]) as wait_ports:
                        cleaned = LAUNCHER_CONTROLLER.cleanup_project_port_owners(Path(temp_dir))

        self.assertEqual(cleaned, [project_owner])
        terminate.assert_called_once_with(123)
        wait_ports.assert_called_once_with([8181, 8989])

    def test_cleanup_project_port_owners_reports_project_port_still_listening_after_cleanup(self):
        project_owner = LAUNCHER_CONTROLLER.PortOwner(
            port=8181,
            pid=123,
            name="WorkWechatProviderServer.exe",
            exe="C:/WorkWechatProviderServer.exe",
            command_line="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(LAUNCHER_CONTROLLER, "iter_listening_port_owners", return_value=[project_owner]):
                with patch.object(LAUNCHER_CONTROLLER, "terminate_process_tree_by_pid"):
                    with patch.object(LAUNCHER_CONTROLLER, "wait_for_ports_released", return_value=[project_owner]):
                        with self.assertRaises(LAUNCHER_CONTROLLER.ProviderPortCleanupError) as raised:
                            LAUNCHER_CONTROLLER.cleanup_project_port_owners(Path(temp_dir))

        self.assertEqual(raised.exception.owner, project_owner)
        self.assertIn("端口在回收后仍未释放", str(raised.exception))

    def test_cleanup_project_port_owners_reports_foreign_conflict(self):
        foreign_owner = LAUNCHER_CONTROLLER.PortOwner(
            port=8181,
            pid=789,
            name="node.exe",
            exe="C:/Program Files/nodejs/node.exe",
            command_line="node server.js",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(LAUNCHER_CONTROLLER, "iter_listening_port_owners", return_value=[foreign_owner]):
                with patch.object(LAUNCHER_CONTROLLER, "terminate_process_tree_by_pid") as terminate:
                    with self.assertRaises(LAUNCHER_CONTROLLER.ProviderPortConflictError) as raised:
                        LAUNCHER_CONTROLLER.cleanup_project_port_owners(Path(temp_dir))

        self.assertEqual(raised.exception.conflicts, [foreign_owner])
        terminate.assert_not_called()

    def test_cleanup_project_port_owners_reports_cleanup_failure(self):
        project_owner = LAUNCHER_CONTROLLER.PortOwner(
            port=8989,
            pid=456,
            name="WorkWechatHookRuntime.exe",
            exe="C:/WorkWechatHookRuntime.exe",
            command_line="",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(LAUNCHER_CONTROLLER, "iter_listening_port_owners", return_value=[project_owner]):
                with patch.object(LAUNCHER_CONTROLLER, "terminate_process_tree_by_pid", side_effect=OSError("denied")):
                    with self.assertRaises(LAUNCHER_CONTROLLER.ProviderPortCleanupError) as raised:
                        LAUNCHER_CONTROLLER.cleanup_project_port_owners(Path(temp_dir))

        self.assertEqual(raised.exception.owner, project_owner)
        self.assertIn("denied", str(raised.exception))

    def test_controller_stop_terminates_packaged_parent_tree_and_waits_for_ports(self):
        class FakeProcess:
            pid = 456

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            controller = LAUNCHER_CONTROLLER.ProviderProcessController(Path(temp_dir), Path(temp_dir))
            controller.process = FakeProcess()
            with patch.object(LAUNCHER_CONTROLLER, "terminate_process_tree_by_pid") as terminate:
                with patch.object(LAUNCHER_CONTROLLER, "cleanup_project_port_owners", return_value=[]) as cleanup:
                    controller.stop()

        terminate.assert_called_once_with(456, timeout=8)
        cleanup.assert_called_once()
        self.assertIsNone(controller.process)

    def test_controller_stop_cleans_residual_ports_when_process_reference_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            controller = LAUNCHER_CONTROLLER.ProviderProcessController(Path(temp_dir), Path(temp_dir))
            controller.process = None
            with patch.object(LAUNCHER_CONTROLLER, "cleanup_project_port_owners", return_value=[]) as cleanup:
                controller.stop()

        cleanup.assert_called_once()
        self.assertIsNone(controller.process)

    def test_launcher_app_imports_when_pyside2_available(self):
        try:
            import PySide2  # noqa: F401
        except ImportError:
            self.skipTest("PySide2 is only required for Windows launcher packaging")

        self.assertTrue(hasattr(LAUNCHER_APP, "LauncherWindow"))

    def test_launcher_buttons_define_pressed_and_disabled_feedback(self):
        # 启动器按钮需要明确的按下和禁用反馈；否则现场点击“启动/重启”时会误以为没有响应。
        self.assertIn("QPushButton#PrimaryButton:pressed", LAUNCHER_APP.APP_STYLE)
        self.assertIn("QPushButton#PrimaryButton:disabled", LAUNCHER_APP.APP_STYLE)
        self.assertIn("QPushButton#SecondaryButton:pressed", LAUNCHER_APP.APP_STYLE)
        self.assertIn("QPushButton#SecondaryButton:disabled", LAUNCHER_APP.APP_STYLE)

    def test_normalize_provider_health_maps_ok_to_running(self):
        result = LAUNCHER_CHECKS.normalize_provider_health({"ok": True, "hook": {"ok": True}, "callback_configured": False})

        self.assertEqual(result["provider_state"], "running")
        self.assertEqual(result["hook_state"], "running")
        self.assertEqual(result["callback_state"], "unconfigured")

    def test_build_autostart_command_quotes_launcher_path(self):
        command = LAUNCHER_APP.build_autostart_command(
            Path("C:/Program Files/WorkWechatProvider/WorkWechatProvider.exe")
        )

        self.assertIn('"C:/Program Files/WorkWechatProvider/WorkWechatProvider.exe"', command)

    def test_auto_start_only_when_workwechat_ready(self):
        ready = LAUNCHER_STATE.LauncherStatus(workwechat_state="ready")
        missing = LAUNCHER_STATE.LauncherStatus(workwechat_state="missing")

        self.assertTrue(LAUNCHER_APP.should_auto_start_from_status(ready))
        self.assertFalse(LAUNCHER_APP.should_auto_start_from_status(missing))

    def test_save_cloud_authorization_config_enables_cloud_without_channel_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"

            saved = LAUNCHER_APP.save_cloud_authorization_config(
                config_path,
                "https://cloud.example.com/",
                "wwauth-demo123",
            )

        self.assertTrue(saved["cloud_poll_enabled"])
        self.assertEqual(saved["cloud_base_url"], "https://cloud.example.com")
        self.assertEqual(saved["authorization_code"], "wwauth-demo123")
        self.assertNotIn("channel_id", saved)
        self.assertNotIn("bot_id", saved)

    def test_save_cloud_authorization_config_clears_token_when_authorization_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(
                """
                {
                  "cloud_poll_enabled": true,
                  "cloud_base_url": "https://cloud.example.com",
                  "authorization_code": "",
                  "client_token": "wwclient-old",
                  "cloud_channel_id": 12
                }
                """,
                encoding="utf-8",
            )

            saved = LAUNCHER_APP.save_cloud_authorization_config(
                config_path,
                "https://cloud.example.com",
                "wwauth-new123",
            )

        self.assertEqual(saved["authorization_code"], "wwauth-new123")
        self.assertEqual(saved["client_token"], "")
        self.assertIsNone(saved["cloud_channel_id"])

    def test_save_cloud_authorization_config_rejects_url_as_authorization_code(self):
        # 启动器现场配置时云端地址和授权码相邻，最常见误操作是把云端地址粘到授权码输入框；
        # 这里提前拦截，避免 provider 后台反复拿 URL 注册并一直停留在“待注册”。
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"

            with self.assertRaisesRegex(ValueError, "authorization_code_invalid"):
                LAUNCHER_APP.save_cloud_authorization_config(
                    config_path,
                    "https://cloud.example.com",
                    "https://cloud.example.com/",
                )

    def test_save_cloud_authorization_config_requires_http_cloud_base_url(self):
        # 云端注册接口需要完整 HTTP(S) 基址；只填 IP 或域名时无法拼出稳定的注册 URL。
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"

            with self.assertRaisesRegex(ValueError, "cloud_base_url_invalid"):
                LAUNCHER_APP.save_cloud_authorization_config(
                    config_path,
                    "cloud.example.com",
                    "wwauth-demo123",
                )

    def test_build_cloud_registration_label_uses_client_token_after_code_consumed(self):
        # 授权码兑换成功后会被清空，UI 不能因为 authorization_code 为空就误判成“未配置”。
        label = LAUNCHER_APP.build_cloud_registration_label(
            {
                "authorization_code": "",
                "client_token": "wwclient-demo",
                "cloud_channel_id": 3,
            }
        )

        self.assertEqual(label, "已注册 · 渠道 3")

    def test_build_cloud_registration_label_shows_pending_before_token_exists(self):
        label = LAUNCHER_APP.build_cloud_registration_label(
            {
                "authorization_code": "wwauth-demo",
                "client_token": "",
                "cloud_channel_id": None,
            }
        )

        self.assertEqual(label, "待注册")
