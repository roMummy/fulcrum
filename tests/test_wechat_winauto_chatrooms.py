from __future__ import annotations

import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models.entities import Bot, BotPlugin, Channel, Chatroom, Conversation, GroupPluginScope, Plugin
from app.services.plugin_directory import _load_group_scope_map
from app.services.wechat_winauto_chatrooms import (
    build_wechat_winauto_chatroom_detail,
    build_wechat_winauto_chatroom_list,
    sync_wechat_winauto_chatroom_from_message,
)


class WechatWinAutoChatroomTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        self.db = TestingSession()

        # 机器人测试夹具遵循 SaaS 精简契约，内部关联只依赖主键，不再构造已删除的 slug。
        bot = Bot(name="Bot")
        plugin = Plugin(code="weather", display_name="天气插件")
        self.db.add_all([bot, plugin])
        self.db.flush()

        channel = Channel(
            bot_id=bot.id,
            name="WXAuto",
            channel_type="wechat_winauto",
            webhook_path="/webhooks/2/inbound",
        )
        self.db.add(channel)
        self.db.flush()

        binding = BotPlugin(bot_id=bot.id, plugin_id=plugin.id, status="enabled")
        self.db.add(binding)
        self.db.commit()

        self.bot = bot
        self.plugin = plugin
        self.channel = channel
        self.binding = binding

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_sync_winauto_chatroom_from_message_creates_chatroom_and_scope(self):
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="wechat_winauto",
            external_id="group:BOT-测试",
            title="BOT-测试",
            chat_type="group",
        )
        self.db.add(conversation)
        self.db.flush()

        result = sync_wechat_winauto_chatroom_from_message(
            self.db,
            self.channel,
            {
                "event_key": "evt-001",
                "conversation": {"external_id": "group:BOT-测试", "title": "BOT-测试", "chat_type": "group"},
                "message": {"message_type": "text", "sender_id": "wxauto:小明", "sender_name": "小明", "content": "hello"},
                "details": {"timestamp": "2026-03-31T08:00:00+00:00"},
            },
            conversation=conversation,
        )
        self.db.commit()

        self.assertTrue(result["chatroom_created"])
        chatroom = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="group:BOT-测试").one()
        self.assertEqual(chatroom.name, "BOT-测试")
        self.assertEqual(chatroom.management_status, "enabled")
        self.assertTrue(chatroom.is_active)
        self.assertEqual(chatroom.registration_source, "passive")
        scope_rows = self.db.query(GroupPluginScope).filter_by(channel_id=self.channel.id, chatroom_id=chatroom.id).all()
        self.assertEqual(len(scope_rows), 1)
        self.assertEqual(scope_rows[0].bot_plugin_id, self.binding.id)

    def test_build_winauto_chatroom_views_and_scope_lookup(self):
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="wechat_winauto",
            external_id="group:策略群",
            title="策略群",
            chat_type="group",
        )
        self.db.add(conversation)
        self.db.commit()

        list_view = build_wechat_winauto_chatroom_list(self.db, self.channel)
        self.assertEqual(list_view["total"], 1)
        self.assertEqual(list_view["items"][0]["external_id"], "group:策略群")

        chatroom = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="group:策略群").one()
        detail = build_wechat_winauto_chatroom_detail(self.db, self.channel, chatroom)
        self.assertEqual(detail["name"], "策略群")
        self.assertEqual(detail["bindings"][0]["plugin_code"], "weather")

        class FakeContext:
            chat_type = "group"
            conversation_external_id = "group:策略群"

        scope_map = _load_group_scope_map(self.db, channel=self.channel, context=FakeContext())
        self.assertIn(self.binding.id, scope_map)

    def test_chatroom_detail_prefills_effective_config_json(self):
        self.binding.config_json = json.dumps(
            {"city": "Shanghai", "templates": {"join": "欢迎来到本群"}, "enabled": True},
            ensure_ascii=False,
        )
        conversation = Conversation(
            bot_id=self.bot.id,
            channel_id=self.channel.id,
            channel_type="wechat_winauto",
            external_id="group:配置群",
            title="配置群",
            chat_type="group",
        )
        self.db.add(conversation)
        self.db.commit()

        build_wechat_winauto_chatroom_list(self.db, self.channel)
        chatroom = self.db.query(Chatroom).filter_by(channel_id=self.channel.id, external_id="group:配置群").one()
        scope = self.db.query(GroupPluginScope).filter_by(channel_id=self.channel.id, chatroom_id=chatroom.id).one()
        scope.scope_status = "overridden"
        scope.config_override_json = json.dumps({"templates": {"join": "群级欢迎词"}}, ensure_ascii=False)
        self.db.commit()

        detail = build_wechat_winauto_chatroom_detail(self.db, self.channel, chatroom)
        effective = json.loads(detail["bindings"][0]["group_scope_effective_config_json"])
        self.assertEqual(effective["city"], "Shanghai")
        self.assertEqual(effective["templates"]["join"], "群级欢迎词")


if __name__ == "__main__":
    unittest.main()
