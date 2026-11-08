import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.models.entities import Bot, Channel
from app.core.time import now_shanghai
from app.models.saas import SaasPlan, Tenant, TenantSubscription
from app.services.open_api_apps import OpenApiAppInput, list_authorized_channel_ids, upsert_open_api_app


class OpenApiAppsTestCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        TestingSession = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        with TestingSession() as platform_db:
            tenant = Tenant(code="open-api-app-test", name="开放应用测试租户", status="active")
            platform_db.add(tenant)
            plan = SaasPlan(
                code="open-api-app-test-plan",
                name="开放应用测试套餐",
                status="active",
                bot_limit=2,
                is_public=True,
            )
            platform_db.add(plan)
            platform_db.commit()
            platform_db.refresh(tenant)
            platform_db.refresh(plan)
            platform_db.add(
                TenantSubscription(
                    tenant_id=tenant.id,
                    plan_id=plan.id,
                    status="active",
                    starts_at=now_shanghai(),
                )
            )
            platform_db.commit()
            self.tenant_id = tenant.id

        # 开放应用、机器人和渠道都属于租户业务数据。夹具通过正式租户会话创建，
        # 让服务测试同时覆盖自动补齐 tenant_id 和跨租户默认拒绝的基础约束。
        self.tenant_scope_context = tenant_scope(self.tenant_id)
        self.tenant_scope_context.__enter__()
        self.tenant_db_context = tenant_session(TestingSession)
        self.db = self.tenant_db_context.__enter__()
        # 应用授权测试通过机器人主键绑定资源，不再依赖已删除的 slug 字段。
        bot = Bot(name="Bot")
        self.db.add(bot)
        self.db.flush()
        self.channel = Channel(bot_id=bot.id, name="企微", channel_type="workwechat", is_enabled=True)
        self.db.add(self.channel)
        self.second_channel = Channel(bot_id=bot.id, name="企微2", channel_type="workwechat", is_enabled=True)
        self.db.add(self.second_channel)
        other_bot = Bot(name="Other Bot")
        self.db.add(other_bot)
        self.db.flush()
        self.other_bot_channel = Channel(bot_id=other_bot.id, name="另一个机器人企微", channel_type="workwechat", is_enabled=True)
        self.db.add(self.other_bot_channel)
        self.db.commit()

    def tearDown(self):
        self.tenant_db_context.__exit__(None, None, None)
        self.tenant_scope_context.__exit__(None, None, None)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_upsert_open_api_app_creates_and_preserves_secret_on_blank_update(self):
        app = upsert_open_api_app(
            self.db,
            0,
            OpenApiAppInput(
                name="收发单系统",
                app_key="supplier-order",
                app_secret="secret-001",
                default_channel_id=self.channel.id,
                authorized_channel_ids=(self.second_channel.id, self.other_bot_channel.id),
                webhook_url="http://supplier.test/webhook",
                webhook_secret="webhook-secret",
            ),
        )
        old_secret = app.app_secret_encrypted
        self.assertIsNone(app.bot_id)
        self.assertEqual(set(list_authorized_channel_ids(self.db, app.id)), {self.channel.id, self.second_channel.id, self.other_bot_channel.id})

        updated = upsert_open_api_app(
            self.db,
            app.id,
            OpenApiAppInput(
                name="收发单系统",
                app_key="supplier-order",
                app_secret="",
                default_channel_id=self.channel.id,
                authorized_channel_ids=(self.channel.id,),
                webhook_url="http://supplier.test/new-webhook",
                webhook_secret="webhook-secret-2",
            ),
        )

        self.assertEqual(updated.app_secret_encrypted, old_secret)
        self.assertEqual(updated.webhook_url, "http://supplier.test/new-webhook")
        self.assertIsNone(updated.bot_id)
        self.assertEqual(list_authorized_channel_ids(self.db, app.id), [self.channel.id])

    def test_upsert_open_api_app_rejects_duplicate_app_key(self):
        upsert_open_api_app(
            self.db,
            0,
            OpenApiAppInput(name="A", app_key="same-key", app_secret="secret-a", default_channel_id=self.channel.id),
        )
        with self.assertRaises(ValueError):
            upsert_open_api_app(
                self.db,
                0,
                OpenApiAppInput(name="B", app_key="same-key", app_secret="secret-b", default_channel_id=self.channel.id),
            )


if __name__ == "__main__":
    unittest.main()
