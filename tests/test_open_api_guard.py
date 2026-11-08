from __future__ import annotations

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, OpenApiApp, OpenApiAppChannel
from app.models.saas import SaasPlan, Tenant, TenantSubscription
from app.services.open_api_guard import OpenApiGuardError, guard_open_api_request


class OpenApiGuardTestCase(unittest.TestCase):
    """验证开放接口请求会经过订阅、能力、渠道和额度四层门禁。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        with self.factory() as db:
            tenant = Tenant(code="guard-test", name="门禁测试租户", status="active")
            plan = SaasPlan(
                code="guard-plan",
                name="门禁套餐",
                status="active",
                monthly_api_request_limit=1,
            )
            db.add_all([tenant, plan])
            db.flush()
            db.add(TenantSubscription(tenant_id=tenant.id, plan_id=plan.id, status="active", starts_at=now_shanghai()))
            db.commit()
            self.tenant_id = int(tenant.id)

        self.scope = tenant_scope(self.tenant_id)
        self.scope.__enter__()
        self.db_context = tenant_session(self.factory)
        self.db = self.db_context.__enter__()
        bot = Bot(name="门禁机器人")
        self.db.add(bot)
        self.db.flush()
        self.channel = Channel(bot_id=bot.id, name="门禁渠道", channel_type="wechat_ipad", is_enabled=True)
        self.db.add(self.channel)
        self.db.flush()
        self.app = OpenApiApp(name="门禁应用", app_key="guard-app", status="active", default_channel_id=self.channel.id)
        self.db.add(self.app)
        self.db.flush()
        self.db.add(OpenApiAppChannel(app_id=self.app.id, channel_id=self.channel.id))
        self.db.commit()

    def tearDown(self) -> None:
        self.db_context.__exit__(None, None, None)
        self.scope.__exit__(None, None, None)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_authorized_request_passes_and_reserves_api_quota(self) -> None:
        result = guard_open_api_request(
            self.db,
            app=self.app,
            request_id="guard-1",
            capability="message.send.text",
            channel_id=self.channel.id,
        )
        self.assertFalse(result.reservation.idempotent)
        self.assertEqual(result.request_id, "guard-1")

    def test_unauthorized_channel_and_unknown_capability_are_rejected(self) -> None:
        with self.assertRaisesRegex(OpenApiGuardError, "channel_not_allowed"):
            guard_open_api_request(
                self.db,
                app=self.app,
                request_id="guard-channel",
                capability="message.send.text",
                channel_id=99999,
            )
        with self.assertRaisesRegex(OpenApiGuardError, "capability_not_supported"):
            guard_open_api_request(
                self.db,
                app=self.app,
                request_id="guard-capability",
                capability="not.real",
                channel_id=self.channel.id,
            )

    def test_subscription_and_quota_errors_are_stable(self) -> None:
        first = guard_open_api_request(self.db, app=self.app, request_id="guard-quota-1")
        self.db.commit()
        self.assertFalse(first.reservation.idempotent)
        with self.assertRaisesRegex(OpenApiGuardError, "quota_exceeded"):
            guard_open_api_request(self.db, app=self.app, request_id="guard-quota-2")

        subscription = self.db.query(TenantSubscription).one()
        subscription.status = "expired"
        self.db.commit()
        with self.assertRaisesRegex(OpenApiGuardError, "subscription_required"):
            guard_open_api_request(self.db, app=self.app, request_id="guard-expired")


if __name__ == "__main__":
    unittest.main()
