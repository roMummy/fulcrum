from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel
from app.models.saas import BotBinding, DeviceTrialClaim, PlatformAuditLog, PlatformUser, SaasPlan, Tenant, TenantSubscription
from app.services.bot_entitlements import (
    BotEntitlementError,
    add_verified_bot_binding,
    bind_bot_account,
    get_bot_quota,
    hash_device_fingerprint,
    list_bot_bindings,
    release_bot_binding_by_platform,
)


@pytest.fixture()
def db(tmp_path):
    """通用额度测试使用隔离 SQLite 数据库，设备原始指纹不会写入开发环境。"""

    engine = create_engine(f"sqlite:///{tmp_path / 'bot-entitlements.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_entitlements(db: Session) -> dict[str, object]:
    """创建试用租户、付费租户、额度套餐和平台管理员。"""

    trial_plan = SaasPlan(
        code="trial",
        name="免费试用",
        provider_node_limit=1,
        bot_limit=1,
        trial_days=7,
        status="active",
    )
    paid_plan = SaasPlan(
        code="standard",
        name="标准版",
        provider_node_limit=3,
        bot_limit=3,
        monthly_price_cents=25_900,
        yearly_price_cents=259_000,
        is_public=True,
        status="active",
    )
    trial_tenant = Tenant(code="trial-tenant", name="试用企业", status="active")
    paid_tenant = Tenant(code="paid-tenant", name="付费企业", status="active")
    other_tenant = Tenant(code="other-tenant", name="其他企业", status="active")
    reviewer = PlatformUser(
        username="binding-admin",
        display_name="绑定管理员",
        password_hash="not-used-in-service-test",
        role="platform_admin",
        must_change_password=False,
    )
    db.add_all([trial_plan, paid_plan, trial_tenant, paid_tenant, other_tenant, reviewer])
    db.flush()
    current = now_shanghai()
    db.add_all(
        [
            TenantSubscription(
                tenant_id=trial_tenant.id,
                plan_id=trial_plan.id,
                status="active",
                starts_at=current,
                expires_at=current + timedelta(days=7),
            ),
            TenantSubscription(
                tenant_id=paid_tenant.id,
                plan_id=paid_plan.id,
                status="active",
                starts_at=current,
                expires_at=current + timedelta(days=30),
            ),
            TenantSubscription(
                tenant_id=other_tenant.id,
                plan_id=paid_plan.id,
                status="active",
                starts_at=current,
                expires_at=current + timedelta(days=30),
            ),
        ]
    )
    db.commit()
    return {
        "trial_tenant": trial_tenant,
        "paid_tenant": paid_tenant,
        "other_tenant": other_tenant,
        "reviewer": reviewer,
    }


def test_trial_binding_hashes_device_and_cannot_exceed_one_bot(db: Session) -> None:
    """试用只允许一个机器人，数据库只保存设备摘要并创建永久试用领取记录。"""

    seeded = _seed_entitlements(db)
    tenant = seeded["trial_tenant"]
    raw_fingerprint = "desktop-device-trial-001"
    with tenant_scope(tenant.id):
        binding = bind_bot_account(
            db,
            adapter_type="WorkWechat",
            bot_account_id="bot-trial-001",
            device_fingerprint=raw_fingerprint,
            display_name="试用机器人",
        )
        quota = get_bot_quota(db)
        with pytest.raises(BotEntitlementError, match="bot_quota_exceeded"):
            bind_bot_account(
                db,
                adapter_type="telegram",
                bot_account_id="bot-trial-002",
                device_fingerprint=raw_fingerprint,
            )

    claim = db.scalar(select(DeviceTrialClaim))
    assert binding.adapter_type == "workwechat"
    assert binding.device_fingerprint_hash == hash_device_fingerprint(raw_fingerprint)
    assert raw_fingerprint not in binding.device_fingerprint_hash
    assert claim.device_fingerprint_hash == binding.device_fingerprint_hash
    assert quota.total == 1
    assert quota.used == 1
    assert quota.remaining == 0


def test_released_binding_can_rebind_same_account(db: Session) -> None:
    """释放后的绑定不再占用账号身份，可以立即重新接入。"""

    seeded = _seed_entitlements(db)
    paid = seeded["paid_tenant"]
    with tenant_scope(paid.id):
        first = bind_bot_account(
            db,
            adapter_type="generic-api",
            bot_account_id="rebindable-account",
            device_fingerprint="rebind-device-a",
        )
        first.status = "released"
        db.commit()

        second = bind_bot_account(
            db,
            adapter_type="generic-api",
            bot_account_id="rebindable-account",
            device_fingerprint="rebind-device-b",
        )
        # 唯一约束要求复用原绑定行，释放信息被清空并刷新为当前设备。
        assert second.id == first.id
        assert second.status == "active"
        assert second.released_at is None
        assert second.release_reason is None
        assert second.device_fingerprint_hash == hash_device_fingerprint("rebind-device-b")


def test_bot_account_and_device_cannot_cross_tenants(db: Session) -> None:
    """机器人账号与设备归属均为全局边界，不能通过换企业重复绑定。"""

    seeded = _seed_entitlements(db)
    paid = seeded["paid_tenant"]
    other = seeded["other_tenant"]
    fingerprint = "shared-device-ownership"
    with tenant_scope(paid.id):
        bind_bot_account(
            db,
            adapter_type="generic-api",
            bot_account_id="global-bot-account",
            device_fingerprint=fingerprint,
        )

    with tenant_scope(other.id):
        with pytest.raises(BotEntitlementError, match="bot_account_already_bound"):
            bind_bot_account(
                db,
                adapter_type="generic-api",
                bot_account_id="global-bot-account",
                device_fingerprint="other-device",
            )
        with pytest.raises(BotEntitlementError, match="device_owned_by_other_tenant"):
            bind_bot_account(
                db,
                adapter_type="telegram",
                bot_account_id="different-bot-account",
                device_fingerprint=fingerprint,
            )


def test_paid_tenant_can_bind_multiple_bots_on_same_device_within_quota(db: Session) -> None:
    """付费企业可在同一设备上绑定多个机器人，但不能超过套餐额度。"""

    seeded = _seed_entitlements(db)
    paid = seeded["paid_tenant"]
    with tenant_scope(paid.id):
        for index in range(3):
            bind_bot_account(
                db,
                adapter_type="generic-api",
                bot_account_id=f"paid-bot-{index}",
                device_fingerprint="paid-shared-device",
            )
        assert len(list_bot_bindings(db)) == 3
        with pytest.raises(BotEntitlementError, match="bot_quota_exceeded"):
            bind_bot_account(
                db,
                adapter_type="generic-api",
                bot_account_id="paid-bot-over-limit",
                device_fingerprint="paid-shared-device",
            )


def test_expired_subscription_cannot_add_binding(db: Session) -> None:
    """订阅到期后保留历史绑定和数据，但不能新增机器人账号。"""

    seeded = _seed_entitlements(db)
    paid = seeded["paid_tenant"]
    subscription = db.scalar(
        select(TenantSubscription).where(TenantSubscription.tenant_id == paid.id)
    )
    subscription.expires_at = now_shanghai() - timedelta(seconds=1)
    db.commit()

    with tenant_scope(paid.id):
        with pytest.raises(BotEntitlementError, match="subscription_expired"):
            bind_bot_account(
                db,
                adapter_type="generic-api",
                bot_account_id="expired-bot",
                device_fingerprint="expired-device",
            )


def test_verified_binding_participates_in_outer_transaction(db: Session) -> None:
    """账号接入失败时，机器人、渠道、额度绑定和试用领取必须一起回滚。"""

    seeded = _seed_entitlements(db)
    tenant = seeded["trial_tenant"]
    with tenant_scope(tenant.id):
        bot = Bot(
            tenant_id=tenant.id,
            name="事务机器人",
            status="stopped",
            is_enabled=False,
        )
        db.add(bot)
        db.flush()
        channel = Channel(
            tenant_id=tenant.id,
            bot_id=bot.id,
            name="事务渠道",
            channel_type="telegram",
            status="draft",
            is_enabled=False,
        )
        db.add(channel)
        db.flush()
        binding = add_verified_bot_binding(
            db,
            adapter_type="telegram",
            bot_account_id="transaction-bot-001",
            device_fingerprint_hash=hash_device_fingerprint("transaction-device-001"),
            display_name="事务机器人",
            bot_id=bot.id,
            channel_id=channel.id,
        )
        assert binding.id is not None
        assert binding.bot_id == bot.id
        assert binding.channel_id == channel.id
        db.rollback()

    assert db.scalar(select(BotBinding.id)) is None
    assert db.scalar(select(DeviceTrialClaim.id)) is None
    assert db.scalar(select(Bot.id).where(Bot.name == "事务机器人")) is None


def test_platform_release_preserves_history_audit_and_trial_claim(db: Session) -> None:
    """平台释放保留绑定记录、原因和试用领取记录，试用租户不能借释放重新领取。"""

    seeded = _seed_entitlements(db)
    trial = seeded["trial_tenant"]
    reviewer = seeded["reviewer"]
    with tenant_scope(trial.id):
        binding = bind_bot_account(
            db,
            adapter_type="generic-api",
            bot_account_id="trial-release-bot",
            device_fingerprint="trial-release-device",
        )

    released = release_bot_binding_by_platform(
        db,
        binding_id=binding.id,
        reviewer_user_id=reviewer.id,
        reason="客户确认设备报废",
    )
    assert released.status == "released"
    assert released.release_reason == "客户确认设备报废"
    assert db.scalar(select(func.count(DeviceTrialClaim.id))) == 1
    assert db.scalar(
        select(func.count(PlatformAuditLog.id)).where(PlatformAuditLog.action == "bot_binding.release")
    ) == 1

    with tenant_scope(trial.id):
        with pytest.raises(BotEntitlementError, match="trial_device_already_claimed"):
            bind_bot_account(
                db,
                adapter_type="telegram",
                bot_account_id="trial-replacement-bot",
                device_fingerprint="trial-release-device",
            )
