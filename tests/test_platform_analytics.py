from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.time import SHANGHAI_TZ
from app.models.entities import Bot, Channel, Conversation, Message, MessageActivityHourly
from app.models.saas import (
    BillingOrder,
    BotBinding,
    ProviderNode,
    SaasPlan,
    Tenant,
    TenantSession,
    TenantSubscription,
    TenantUser,
)
from app.services.platform_analytics import build_platform_analytics, normalize_analytics_range


@pytest.fixture()
def db(tmp_path):
    """平台分析测试使用完整独立数据库，确保跨租户统计不依赖主应用运行状态。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'platform-analytics.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_platform_analytics_normalizes_range_and_keeps_complete_empty_buckets(db: Session) -> None:
    """非法范围必须收敛为七天，空平台也要返回稳定时间轴和零值指标。"""

    now = datetime(2026, 8, 27, 12, 30, tzinfo=SHANGHAI_TZ)
    analytics = build_platform_analytics(db, range_key="unsupported", now=now)

    assert normalize_analytics_range("24h") == "24h"
    assert normalize_analytics_range("30d") == "30d"
    assert normalize_analytics_range("unsupported") == "7d"
    assert analytics["range"] == "7d"
    assert len(analytics["message_trend"]["items"]) == 7
    assert len(analytics["activity_trend"]["items"]) == 7
    assert all(item["total"] == 0 for item in analytics["message_trend"]["items"])
    assert analytics["core_metrics"]["message_total"] == 0
    assert analytics["channel_summary"] == []
    assert analytics["tenant_rankings"] == []


def test_platform_analytics_builds_saas_usage_operations_and_health_view(db: Session) -> None:
    """一组跨租户样本应同时生成使用、经营、在线和运营介入所需的完整口径。"""

    now = datetime(2026, 8, 27, 12, 30, tzinfo=SHANGHAI_TZ)
    paid_plan = SaasPlan(
        code="paid",
        name="标准版",
        provider_node_limit=1,
        bot_limit=1,
        monthly_price_cents=25_900,
        yearly_price_cents=259_000,
        trial_days=0,
        status="active",
    )
    trial_plan = SaasPlan(
        code="trial",
        name="试用版",
        provider_node_limit=1,
        bot_limit=2,
        monthly_price_cents=0,
        yearly_price_cents=0,
        trial_days=7,
        status="active",
    )
    alpha = Tenant(code="alpha", name="甲企业", status="active")
    beta = Tenant(code="beta", name="乙企业", status="active")
    gamma = Tenant(code="gamma", name="丙企业", status="disabled")
    delta = Tenant(code="delta", name="丁企业", status="active")
    db.add_all([paid_plan, trial_plan, alpha, beta, gamma, delta])
    db.flush()

    db.add_all(
        [
            TenantSubscription(
                tenant_id=alpha.id,
                plan_id=paid_plan.id,
                status="active",
                starts_at=now - timedelta(days=40),
                expires_at=now + timedelta(days=30),
            ),
            TenantSubscription(
                tenant_id=beta.id,
                plan_id=trial_plan.id,
                status="active",
                starts_at=now - timedelta(days=4),
                expires_at=now + timedelta(days=3),
            ),
            TenantSubscription(
                tenant_id=gamma.id,
                plan_id=paid_plan.id,
                status="suspended",
                starts_at=now - timedelta(days=60),
                expires_at=now - timedelta(days=1),
            ),
            TenantSubscription(
                tenant_id=delta.id,
                plan_id=trial_plan.id,
                status="active",
                starts_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=10),
            ),
        ]
    )

    alpha_bot = Bot(tenant_id=alpha.id, name="甲机器人", status="running", is_enabled=True)
    beta_bot = Bot(tenant_id=beta.id, name="乙机器人", status="running", is_enabled=True)
    db.add_all([alpha_bot, beta_bot])
    db.flush()
    alpha_channel = Channel(
        tenant_id=alpha.id,
        bot_id=alpha_bot.id,
        name="甲企微账号",
        channel_type="workwechat",
        is_enabled=True,
        status="online",
        login_state="online",
    )
    beta_channel = Channel(
        tenant_id=beta.id,
        bot_id=beta_bot.id,
        name="乙 TG 账号",
        channel_type="telegram",
        is_enabled=True,
        status="degraded",
        login_state="offline",
    )
    db.add_all([alpha_channel, beta_channel])
    db.flush()

    db.add_all(
        [
            BotBinding(
                tenant_id=alpha.id,
                bot_id=alpha_bot.id,
                channel_id=alpha_channel.id,
                adapter_type="workwechat",
                bot_account_id="alpha-account",
                device_fingerprint_hash="a" * 64,
                status="active",
            ),
            BotBinding(
                tenant_id=beta.id,
                bot_id=beta_bot.id,
                channel_id=beta_channel.id,
                adapter_type="telegram",
                bot_account_id="beta-account",
                device_fingerprint_hash="b" * 64,
                status="active",
            ),
        ]
    )

    alpha_conversation = Conversation(
        tenant_id=alpha.id,
        bot_id=alpha_bot.id,
        channel_id=alpha_channel.id,
        channel_type="workwechat",
        external_id="alpha-conversation",
        chat_type="group",
    )
    beta_conversation = Conversation(
        tenant_id=beta.id,
        bot_id=beta_bot.id,
        channel_id=beta_channel.id,
        channel_type="telegram",
        external_id="beta-conversation",
        chat_type="direct",
    )
    db.add_all([alpha_conversation, beta_conversation])
    db.flush()
    db.add_all(
        [
            Message(
                tenant_id=alpha.id,
                conversation_id=alpha_conversation.id,
                direction="in",
                sender_id="alpha-user-a",
                content="不应进入分析结果的消息正文",
                created_at=now - timedelta(hours=1, minutes=20),
            ),
            Message(
                tenant_id=alpha.id,
                conversation_id=alpha_conversation.id,
                direction="in",
                sender_id="alpha-user-a",
                content="重复用户消息",
                created_at=now - timedelta(hours=1, minutes=10),
            ),
            Message(
                tenant_id=alpha.id,
                conversation_id=alpha_conversation.id,
                direction="in",
                sender_id="alpha-user-b",
                content="第二位用户消息",
                created_at=now - timedelta(hours=1),
            ),
            Message(
                tenant_id=beta.id,
                conversation_id=beta_conversation.id,
                direction="in",
                sender_id="alpha-user-a",
                content="不同租户同标识仍需分别计数",
                created_at=now - timedelta(hours=2),
            ),
        ]
    )
    db.add_all(
        [
            MessageActivityHourly(
                tenant_id=alpha.id,
                bucket_start=now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1),
                bot_id=alpha_bot.id,
                channel_id=alpha_channel.id,
                channel_type="workwechat",
                chat_type="group",
                direction="in",
                message_count=3,
            ),
            MessageActivityHourly(
                tenant_id=alpha.id,
                bucket_start=now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1),
                bot_id=alpha_bot.id,
                channel_id=alpha_channel.id,
                channel_type="workwechat",
                chat_type="group",
                direction="out",
                message_count=2,
            ),
            MessageActivityHourly(
                tenant_id=beta.id,
                bucket_start=now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2),
                bot_id=beta_bot.id,
                channel_id=beta_channel.id,
                channel_type="telegram",
                chat_type="direct",
                direction="in",
                message_count=1,
            ),
        ]
    )

    alpha_user = TenantUser(
        tenant_id=alpha.id,
        username="alpha-owner",
        display_name="甲管理员",
        password_hash="hash",
        role="tenant_admin",
        is_enabled=True,
        auth_version=1,
        must_change_password=False,
    )
    beta_user = TenantUser(
        tenant_id=beta.id,
        username="beta-owner",
        display_name="乙管理员",
        password_hash="hash",
        role="tenant_admin",
        is_enabled=True,
        auth_version=1,
        must_change_password=False,
    )
    db.add_all([alpha_user, beta_user])
    db.flush()
    db.add_all(
        [
            TenantSession(
                tenant_id=alpha.id,
                tenant_user_id=alpha_user.id,
                session_token_hash="1" * 64,
                csrf_token_hash="2" * 64,
                auth_version=1,
                expires_at=now + timedelta(hours=2),
                last_seen_at=now - timedelta(minutes=10),
            ),
            TenantSession(
                tenant_id=beta.id,
                tenant_user_id=beta_user.id,
                session_token_hash="3" * 64,
                csrf_token_hash="4" * 64,
                auth_version=1,
                expires_at=now - timedelta(minutes=1),
                last_seen_at=now - timedelta(minutes=5),
            ),
            ProviderNode(
                tenant_id=alpha.id,
                name="甲执行端",
                status="active",
                device_id="alpha-device",
                last_heartbeat_at=now - timedelta(minutes=2),
            ),
            ProviderNode(
                tenant_id=beta.id,
                name="乙执行端",
                status="active",
                device_id="beta-device",
                last_heartbeat_at=now - timedelta(minutes=20),
            ),
            BillingOrder(
                order_no="ORDER-PENDING",
                tenant_id=beta.id,
                plan_id=paid_plan.id,
                billing_cycle="monthly",
                bot_limit_snapshot=1,
                amount_cents=25_900,
                status="payment_submitted",
                payment_submitted_at=now - timedelta(hours=3),
            ),
            BillingOrder(
                order_no="ORDER-APPROVED",
                tenant_id=alpha.id,
                plan_id=paid_plan.id,
                billing_cycle="monthly",
                bot_limit_snapshot=1,
                amount_cents=25_900,
                status="approved",
                reviewed_at=now - timedelta(days=2),
            ),
        ]
    )
    db.commit()

    analytics = build_platform_analytics(db, range_key="7d", now=now)
    metrics = analytics["core_metrics"]

    assert metrics["tenant_total"] == 4
    assert metrics["active_tenant_total"] == 2
    assert metrics["message_total"] == 6
    assert metrics["inbound_total"] == 4
    assert metrics["outbound_total"] == 2
    assert metrics["trigger_user_total"] == 3
    assert metrics["connected_account_total"] == 2
    assert metrics["online_channel_total"] == 1
    assert metrics["channel_online_rate"] == 50
    assert metrics["pending_order_total"] == 1

    assert [item["count"] for item in analytics["adoption_funnel"]] == [4, 3, 2, 1, 1]
    assert analytics["channel_summary"][0]["channel_type"] == "workwechat"
    assert analytics["channel_summary"][0]["message_total"] == 5
    assert analytics["channel_summary"][1]["channel_type"] == "telegram"
    assert analytics["tenant_rankings"][0]["tenant_name"] == "甲企业"
    assert analytics["tenant_rankings"][0]["message_total"] == 5
    assert analytics["tenant_rankings"][0]["trigger_user_total"] == 2

    health_by_tenant = {item["tenant_name"]: item["status"] for item in analytics["tenant_health"]}
    assert health_by_tenant["丙企业"] == "subscription_inactive"
    assert health_by_tenant["甲企业"] == "quota_full"
    assert health_by_tenant["丁企业"] == "account_missing"
    assert health_by_tenant["乙企业"] == "channel_offline"

    assert analytics["subscription_summary"]["trial_tenant_total"] == 2
    assert analytics["subscription_summary"]["paid_tenant_total"] == 1
    assert analytics["subscription_summary"]["expiring_soon_total"] == 1
    assert analytics["subscription_summary"]["inactive_total"] == 1
    assert analytics["subscription_summary"]["approved_amount_cents"] == 25_900
    assert analytics["presence_summary"] == {
        "active_employee_total": 1,
        "valid_session_total": 1,
        "online_executor_total": 1,
        "online_channel_total": 1,
        "abnormal_channel_total": 1,
    }

    # 聚合结果只保留数量和租户级运营信息，不能把消息正文、用户标识或设备标识带到模板。
    rendered = repr(analytics)
    assert "alpha-user-a" not in rendered
    assert "不应进入分析结果的消息正文" not in rendered
    assert "alpha-device" not in rendered
