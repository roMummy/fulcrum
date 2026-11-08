from __future__ import annotations

import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel, OpenApiApp, OpenApiUsageRecord
from app.models.saas import SaasPlan, Tenant, TenantSubscription
from app.services.open_api_apps import OpenApiAppInput, upsert_open_api_app
from app.services.open_api_quotas import (
    OpenApiQuotaError,
    finalize_open_api_usage,
    require_open_api_resource_quota,
    reserve_open_api_usage,
    usage_summary,
)


class OpenApiQuotasTestCase(unittest.TestCase):
    """验证开放应用资源额度、月度账本和请求幂等行为。"""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        self.factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=self.engine)
        with self.factory() as db:
            tenant = Tenant(code="quota-test", name="额度测试租户", status="active")
            plan = SaasPlan(
                code="quota-plan",
                name="额度套餐",
                status="active",
                bot_limit=2,
                open_api_app_limit=1,
                open_api_channel_limit=2,
                monthly_api_request_limit=2,
                monthly_message_limit=1,
                monthly_media_bytes_limit=10,
            )
            db.add_all([tenant, plan])
            db.flush()
            db.add(
                TenantSubscription(
                    tenant_id=tenant.id,
                    plan_id=plan.id,
                    status="active",
                    starts_at=now_shanghai(),
                )
            )
            db.commit()
            self.tenant_id = int(tenant.id)

        self.scope = tenant_scope(self.tenant_id)
        self.scope.__enter__()
        self.db_context = tenant_session(self.factory)
        self.db = self.db_context.__enter__()
        bot = Bot(name="额度机器人")
        self.db.add(bot)
        self.db.flush()
        self.channel = Channel(bot_id=bot.id, name="额度渠道", channel_type="workwechat", is_enabled=True)
        self.second_channel = Channel(bot_id=bot.id, name="额度渠道2", channel_type="workwechat", is_enabled=True)
        self.db.add_all([self.channel, self.second_channel])
        self.db.flush()
        self.app = upsert_open_api_app(
            self.db,
            0,
            OpenApiAppInput(
                name="额度应用",
                app_key="quota-app",
                app_secret="secret",
                default_channel_id=self.channel.id,
                authorized_channel_ids=(self.second_channel.id,),
            ),
        )

    def tearDown(self) -> None:
        self.db_context.__exit__(None, None, None)
        self.scope.__exit__(None, None, None)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_same_request_id_only_reserves_once_and_commit_consumes_units(self) -> None:
        first = reserve_open_api_usage(
            self.db,
            app=self.app,
            request_id="req-1",
            usage={"api_requests": 1, "messages": 1},
        )
        self.assertFalse(first.idempotent)
        self.db.commit()

        repeated = reserve_open_api_usage(
            self.db,
            app=self.app,
            request_id="req-1",
            usage={"api_requests": 1, "messages": 1},
        )
        self.assertTrue(repeated.idempotent)
        self.assertEqual({record.id for record in first.records}, {record.id for record in repeated.records})
        finalize_open_api_usage(self.db, reservation=first, status="committed")
        self.db.commit()

        summary = usage_summary(self.db, tenant_id=self.tenant_id)
        self.assertEqual(summary["api_requests_used"], 1)
        self.assertEqual(summary["api_requests_remaining"], 1)
        self.assertEqual(summary["messages_used"], 1)

    def test_failed_request_can_refund_reserved_units(self) -> None:
        reservation = reserve_open_api_usage(
            self.db,
            app=self.app,
            request_id="req-refund",
            usage={"api_requests": 1, "media_bytes": 10},
        )
        finalize_open_api_usage(self.db, reservation=reservation, status="refunded")
        self.db.commit()

        summary = usage_summary(self.db, tenant_id=self.tenant_id)
        self.assertEqual(summary["api_requests_used"], 0)
        self.assertEqual(summary["media_bytes_used"], 0)
        records = self.db.scalars(select(OpenApiUsageRecord).where(OpenApiUsageRecord.request_id == "req-refund")).all()
        self.assertTrue(records)
        self.assertTrue(all(record.status == "refunded" for record in records))

    def test_exhausted_quota_and_resource_limits_are_rejected(self) -> None:
        reserve_open_api_usage(self.db, app=self.app, request_id="req-a", usage={"api_requests": 2})
        self.db.commit()
        with self.assertRaisesRegex(OpenApiQuotaError, "quota_exceeded"):
            reserve_open_api_usage(self.db, app=self.app, request_id="req-b", usage={"api_requests": 1})

        with self.assertRaisesRegex(OpenApiQuotaError, "quota_exceeded"):
            require_open_api_resource_quota(
                self.db,
                tenant_id=self.tenant_id,
                app_id=None,
                requested_channel_ids={self.channel.id},
            )


if __name__ == "__main__":
    unittest.main()
