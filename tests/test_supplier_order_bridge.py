import importlib
import unittest
from types import SimpleNamespace

from app.services.plugin_runtime.contracts import PluginExecutionRequest
from app.services.plugin_runtime import get_plugin_runtime_meta


class SupplierOrderBridgePluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = importlib.import_module("plugins.supplier_order_flow.plugin")

    def _context(self, **overrides):
        values = {
            "channel_type": "workwechat",
            "chat_type": "group",
            "content": "",
            "attachments": (),
            "conversation_external_id": "R:supplier-room",
            "binding_config": {},
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_plugin_is_discovered_without_old_admin_surface(self):
        meta = get_plugin_runtime_meta("supplier_order_flow")

        self.assertTrue(meta["manifest_valid"])
        self.assertEqual(meta["plugin_code"], "supplier_order_flow")
        self.assertFalse(meta["admin_extension"]["enabled"])

    def test_help_command_returns_reply_text(self):
        context = self._context(content="供应商帮助")

        match = self.plugin.matches_event(context)
        result = self.plugin.handle_event(context)

        self.assertTrue(match["matched"])
        self.assertEqual(match["reason"], "supplier_order_command")
        self.assertTrue(result["ok"])
        self.assertIn("reply_text", result["output"])
        self.assertIn("supplier-order-admin", result["output"]["reply_text"])

    def test_excel_and_text_candidates_do_not_auto_reply(self):
        excel_context = self._context(attachments=({"file_name": "回传.xls"},))
        text_context = self._context(content="分单ID: SPLIT-1 快递公司: 圆通 快递单号: YT1234567890123")

        excel_result = self.plugin.handle_event(excel_context)
        text_result = self.plugin.handle_event(text_context)

        self.assertTrue(self.plugin.matches_event(excel_context)["matched"])
        self.assertTrue(self.plugin.matches_event(text_context)["matched"])
        self.assertNotIn("reply_text", excel_result["output"])
        self.assertNotIn("reply_text", text_result["output"])

    def test_disabled_conversation_is_not_matched(self):
        context = self._context(
            content="供应商帮助",
            conversation_external_id="R:disabled-room",
            binding_config={"disabled_conversation_external_ids": ["R:disabled-room"]},
        )

        match = self.plugin.matches_event(context)

        self.assertFalse(match["matched"])
        self.assertEqual(match["reason"], "supplier_order_flow_group_disabled")

    def test_runtime_reload_reports_bridge_only_state(self):
        result = self.plugin.execute_action(
            PluginExecutionRequest(
                action="reload",
                config={
                    "supplier_order_admin_url": "http://example.test/supplier-order-admin",
                    "disabled_conversation_external_ids": ["R:disabled-room"],
                },
                payload={},
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["output"]["bridge_only"])
        self.assertEqual(result["output"]["disabled_conversation_external_ids"], ["R:disabled-room"])


if __name__ == "__main__":
    unittest.main()
