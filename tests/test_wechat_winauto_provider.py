from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "vendors" / "wechat_winauto_provider" / "app.py"
SPEC = importlib.util.spec_from_file_location("wechat_winauto_provider_app", MODULE_PATH)
assert SPEC and SPEC.loader
PROVIDER_APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROVIDER_APP
SPEC.loader.exec_module(PROVIDER_APP)


class FakeDownloadedItem:
    def __init__(self, file_name: str, *, return_value, expects_keyword: str | None = "dir_path") -> None:
        self.filename = file_name
        self._return_value = return_value
        self._expects_keyword = expects_keyword
        self.calls: list[object] = []
        if expects_keyword:
            self.download = self._download_keyword
        else:
            self.download = self._download_positional

    def _download_keyword(self, dir_path):
        kwargs = {str(self._expects_keyword): dir_path}
        self.calls.append(kwargs)
        target_dir = Path(dir_path)
        if callable(self._return_value):
            return self._return_value(target_dir)
        return self._return_value

    def _download_positional(self, save_path):
        self.calls.append((save_path,))
        target_dir = Path(save_path)
        if callable(self._return_value):
            return self._return_value(target_dir)
        return self._return_value


class WechatWinAutoProviderDownloadTests(unittest.TestCase):
    def _build_client(self, media_root: Path):
        config = PROVIDER_APP.ProviderConfig(
            media_download_dir=str(media_root),
            media_download_async=False,
            media_download_initial_delay_seconds=0,
            media_download_retry_count=0,
            media_download_retry_delay_seconds=0,
        )
        return PROVIDER_APP.WxAutoClient(config)

    def test_download_media_details_resolves_file_from_returned_path_string(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            client = self._build_client(media_root)

            def write_file(target_dir: Path):
                file_path = target_dir / "报价单.pdf"
                file_path.write_bytes(b"demo")
                return str(file_path)

            item = FakeDownloadedItem("报价单.pdf", return_value=write_file)
            details = client._download_media_details(item, message_type="file", event_key="evt-1")

            self.assertTrue(str(details.get("download_path") or "").endswith("报价单.pdf"))
            self.assertEqual(details.get("file_name"), "报价单.pdf")
            self.assertEqual(item.calls[0], {"dir_path": str((media_root / "file").resolve())})

    def test_download_media_details_finds_new_file_when_library_returns_success_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            client = self._build_client(media_root)

            def write_file(target_dir: Path):
                file_path = target_dir / "合同.docx"
                file_path.write_bytes(b"doc")
                return "下载成功"

            item = FakeDownloadedItem("合同.docx", return_value=write_file)
            details = client._download_media_details(item, message_type="file", event_key="evt-2")

            self.assertTrue(str(details.get("download_path") or "").endswith("合同.docx"))
            self.assertEqual(details.get("file_name"), "合同.docx")
            attempts = details.get("download_attempts") or []
            self.assertEqual(attempts[0].get("resolved_path"), str((media_root / "file" / "合同.docx").resolve()))

    def test_invoke_media_download_supports_single_positional_path_signature(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media_root = Path(temp_dir)
            client = self._build_client(media_root)

            def write_file(target_dir: Path):
                file_path = target_dir / "图片.png"
                file_path.write_bytes(b"png")
                return "ok"

            item = FakeDownloadedItem("图片.png", return_value=write_file, expects_keyword=None)
            details = client._download_media_details(item, message_type="image", event_key="evt-3")

            self.assertTrue(str(details.get("download_path") or "").endswith("图片.png"))
            self.assertEqual(item.calls[0], (str((media_root / "image").resolve()),))

    def test_build_event_key_keeps_text_hash_stable(self):
        client = self._build_client(Path(tempfile.gettempdir()))

        key1 = client._build_event_key(
            "BOT-测试",
            "水牛",
            "同一条文本",
            "2026-03-31T08:00:00+00:00",
            "text",
            message_hash="same-hash",
        )
        key2 = client._build_event_key(
            "BOT-测试",
            "水牛",
            "同一条文本",
            "2026-03-31T09:00:00+00:00",
            "text",
            message_hash="same-hash",
        )

        self.assertEqual(key1, key2)

    def test_build_event_key_distinguishes_repeated_file_messages(self):
        client = self._build_client(Path(tempfile.gettempdir()))

        key1 = client._build_event_key(
            "BOT-测试",
            "水牛",
            "文件\n测试excel.xls\n微信电脑版",
            "2026-03-30T09:40:45+00:00",
            "file",
            message_hash="same-hash",
            file_name="测试excel.xls",
        )
        key2 = client._build_event_key(
            "BOT-测试",
            "水牛",
            "文件\n测试excel.xls\n微信电脑版",
            "2026-03-31T08:07:37+00:00",
            "file",
            message_hash="same-hash",
            file_name="测试excel.xls",
        )

        self.assertNotEqual(key1, key2)

    def test_auto_expand_registers_session_chats_beyond_seed_list(self):
        config = PROVIDER_APP.ProviderConfig(
            listen_chats=["种子群"],
            auto_expand_listen=True,
            auto_expand_scan_interval_seconds=0,
            auto_expand_max_chats=10,
        )
        client = PROVIDER_APP.WxAutoClient(config)

        class FakeWx:
            def __init__(self):
                self.added = []

            def ChatWith(self, chat_name):
                return chat_name

            def AddListenChat(self, who):
                self.added.append(who)

            def StartListening(self):
                return None

            def GetSessionList(self):
                return [{"chat_name": "自动发现群"}, {"chat_name": "种子群"}]

        client._wx = FakeWx()
        client.start_listening()
        client._last_auto_expand_scan_at = 0
        client._auto_expand_listen_targets(use_callback=True)

        self.assertIn("种子群", client.list_listened_chats())
        self.assertIn("自动发现群", client.list_listened_chats())
        self.assertIn("自动发现群", client.list_auto_discovered_chats())

    def test_auto_expand_registers_session_chats_from_get_session(self):
        config = PROVIDER_APP.ProviderConfig(
            listen_chats=["种子群"],
            auto_expand_listen=True,
            auto_expand_scan_interval_seconds=0,
            auto_expand_max_chats=10,
            fallback_poll_strategy="sessions",
        )
        client = PROVIDER_APP.WxAutoClient(config)

        class FakeSessionElement:
            def __init__(self, name):
                self.name = name

        class FakeWx:
            def __init__(self):
                self.added = []

            def ChatWith(self, chat_name):
                return chat_name

            def GetSession(self):
                return [FakeSessionElement("自动发现群"), FakeSessionElement("种子群")]

        client._wx = FakeWx()
        client.start_listening()
        client._last_auto_expand_scan_at = 0
        client._auto_expand_listen_targets(use_callback=False)

        self.assertTrue(client.fallback_poll_enabled())
        self.assertIn("种子群", client.list_listened_chats())
        self.assertIn("自动发现群", client.list_listened_chats())
        self.assertIn("自动发现群", client.list_auto_discovered_chats())

    def test_extract_chat_descriptor_avoids_deep_serialization_for_session_objects(self):
        client = self._build_client(Path(tempfile.gettempdir()))

        class ExplosiveSessionElement:
            def __init__(self, name):
                object.__setattr__(self, "name", name)

            def __getattribute__(self, item):
                if item == "__dict__":
                    raise RecursionError("boom")
                return object.__getattribute__(self, item)

        descriptor = client._extract_chat_descriptor(ExplosiveSessionElement("自动发现群"))

        self.assertEqual(descriptor.get("chat_name"), "自动发现群")

    def test_health_payload_exposes_auto_expand_config_and_status(self):
        config = PROVIDER_APP.ProviderConfig(
            listen_chats=["种子群"],
            auto_expand_listen=True,
            auto_expand_scan_interval_seconds=15,
            auto_expand_max_chats=20,
        )
        runtime = PROVIDER_APP.ProviderRuntime(config)
        runtime.listen_status.running = True
        runtime.listen_status.seed_chats = ["种子群"]
        runtime.listen_status.listened_chats = ["种子群", "自动发现群"]
        runtime.listen_status.auto_discovered_chats = ["自动发现群"]
        runtime.listen_status.listen_failures = {"失败群": "not found"}

        payload = runtime.health_payload()

        self.assertTrue(payload["config"]["auto_expand_listen"])
        self.assertEqual(payload["config"]["auto_expand_scan_interval_seconds"], 15)
        self.assertEqual(payload["config"]["auto_expand_max_chats"], 20)
        self.assertIn("自动发现群", payload["listen_status"]["auto_discovered_chats"])


if __name__ == "__main__":
    unittest.main()
