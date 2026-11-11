from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.time import now_shanghai
from app.models.entities import Bot, Channel
from app.models.saas import BotBinding, DeviceTrialClaim, PlatformProtocolService, SaasPlan, Tenant, TenantSubscription
from app.services.account_connections.contracts import AccountConnectionError, AccountConnectionResult, VerifiedAccountIdentity
from app.services.account_connections.service import (
    begin_account_connection,
    finalize_account_connection,
    verify_account_connection,
)
from app.services.bot_entitlements import get_bot_quota


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'account-connection-service.db'}")
    Base.metadata.create_all(bind=engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _seed_paid_tenant(db: Session) -> Tenant:
    plan = SaasPlan(
        code="standard",
        name="标准版",
        provider_node_limit=3,
        bot_limit=3,
        monthly_price_cents=25_900,
        yearly_price_cents=259_000,
        status="active",
    )
    tenant = Tenant(code="account-service-tenant", name="连接服务企业", status="active")
    db.add_all([plan, tenant])
    db.flush()
    db.add(
        TenantSubscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            status="active",
            starts_at=now_shanghai(),
            expires_at=now_shanghai() + timedelta(days=30),
        )
    )
    db.commit()
    return tenant


class _VerifiedConnector:
    channel_type = "telegram"

    def __init__(self, account_id: str = "10001"):
        self.account_id = account_id

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        del db, channel
        return AccountConnectionResult.verified(
            VerifiedAccountIdentity(
                adapter_type="telegram",
                account_id=self.account_id,
                display_name="@sales_bot",
                device_key=f"telegram-bot:{self.account_id}",
            )
        )


def test_draft_does_not_consume_quota_and_finalize_is_atomic(db: Session, monkeypatch) -> None:
    tenant = _seed_paid_tenant(db)
    with tenant_scope(tenant.id):
        draft = begin_account_connection(
            db,
            channel_type="telegram",
            display_name="销售机器人",
            connection_values={"bot_token": "token-value"},
        )
        assert draft.status == "draft"
        assert draft.is_enabled is False
        assert get_bot_quota(db).used == 0

        monkeypatch.setattr(
            "app.services.account_connections.service.get_account_connector",
            lambda channel_type: _VerifiedConnector(),
        )
        result = verify_account_connection(db, channel_id=draft.id)
        assert result.state == "verified"
        db.refresh(draft)
        assert "telegram-bot:10001" not in (draft.runtime_info_json or "")

        account = finalize_account_connection(db, channel_id=draft.id, display_name="销售机器人")
        assert account.binding.bot_id == account.bot.id
        assert account.binding.channel_id == account.channel.id
        assert account.bot.is_enabled is True
        assert account.channel.is_enabled is True
        assert get_bot_quota(db).used == 1


def test_duplicate_verified_identity_rolls_back_second_draft(db: Session, monkeypatch) -> None:
    tenant = _seed_paid_tenant(db)
    monkeypatch.setattr(
        "app.services.account_connections.service.get_account_connector",
        lambda channel_type: _VerifiedConnector("duplicate-001"),
    )
    with tenant_scope(tenant.id):
        first = begin_account_connection(
            db,
            channel_type="telegram",
            display_name="第一个账号",
            connection_values={"bot_token": "token-one"},
        )
        verify_account_connection(db, channel_id=first.id)
        finalize_account_connection(db, channel_id=first.id, display_name="第一个账号")

        second = begin_account_connection(
            db,
            channel_type="telegram",
            display_name="第二个账号",
            connection_values={"bot_token": "token-two"},
        )
        verify_account_connection(db, channel_id=second.id)
        with pytest.raises(AccountConnectionError, match="bot_account_already_bound"):
            finalize_account_connection(db, channel_id=second.id, display_name="第二个账号")

    db.expire_all()
    second_channel = db.get(Channel, second.id)
    second_bot = db.get(Bot, second_channel.bot_id)
    assert second_channel.is_enabled is False
    assert second_channel.status == "verified"
    assert second_bot.is_enabled is False
    assert db.scalars(select(BotBinding)).all().__len__() == 1
    assert db.scalars(select(DeviceTrialClaim)).all() == []


def test_unverified_draft_cannot_be_enabled(db: Session) -> None:
    tenant = _seed_paid_tenant(db)
    with tenant_scope(tenant.id):
        draft = begin_account_connection(
            db,
            channel_type="telegram",
            display_name="未验证账号",
            connection_values={"bot_token": "token"},
        )
        with pytest.raises(AccountConnectionError, match="connection_not_verified"):
            finalize_account_connection(db, channel_id=draft.id, display_name="未验证账号")


@pytest.mark.parametrize(
    ("deleted_model", "error"),
    [(Channel, "connection_not_found"), (Bot, "connection_bot_missing")],
    ids=["deleted-channel", "deleted-bot"],
)
def test_deleted_draft_cannot_be_finalized(db: Session, monkeypatch, deleted_model, error) -> None:
    """已验证草稿的渠道或机器人被软删除后，旧确认操作不能重新启用或占用额度。"""

    tenant = _seed_paid_tenant(db)
    monkeypatch.setattr(
        "app.services.account_connections.service.get_account_connector",
        lambda channel_type: _VerifiedConnector(),
    )
    with tenant_scope(tenant.id):
        draft = begin_account_connection(
            db,
            channel_type="telegram",
            display_name="待删除账号",
            connection_values={"bot_token": "test-deleted-draft-token"},
        )
        verify_account_connection(db, channel_id=draft.id)
        bot = db.get(Bot, draft.bot_id)
        deleted = draft if deleted_model is Channel else bot
        deleted.deleted_at = now_shanghai()
        db.commit()

        if deleted_model is Channel:
            with pytest.raises(AccountConnectionError, match="connection_not_found"):
                verify_account_connection(db, channel_id=draft.id)
        with pytest.raises(AccountConnectionError, match=error):
            finalize_account_connection(db, channel_id=draft.id, display_name="旧页面确认")

        db.refresh(draft)
        db.refresh(bot)
        assert draft.is_enabled is False and bot.is_enabled is False
        assert deleted.deleted_at is not None
        assert get_bot_quota(db).used == 0
        assert db.scalar(select(BotBinding).where(BotBinding.channel_id == draft.id)) is None


def test_platform_managed_connection_requires_enabled_service_before_creating_draft(db: Session) -> None:
    """平台服务缺失或暂停时必须在建 Bot 前失败，不能产生孤立草稿。"""

    tenant = _seed_paid_tenant(db)
    with tenant_scope(tenant.id):
        with pytest.raises(AccountConnectionError, match="platform_protocol_service_missing"):
            begin_account_connection(
                db,
                channel_type="wechat_ipad",
                display_name="缺少平台服务",
                connection_values={"api_base": "http://forged.internal:9064"},
            )
        assert db.scalars(select(Bot)).all() == []
        assert db.scalars(select(Channel)).all() == []

        db.add(
            PlatformProtocolService(
                channel_type="wechat_ipad",
                api_base="http://ipad.platform.internal:9064",
                is_enabled=False,
            )
        )
        db.commit()
        with pytest.raises(AccountConnectionError, match="platform_protocol_service_disabled"):
            begin_account_connection(
                db,
                channel_type="wechat_ipad",
                display_name="暂停接入账号",
                connection_values={"api_base": "http://forged.internal:9064"},
            )
        assert db.scalars(select(Bot)).all() == []
        assert db.scalars(select(Channel)).all() == []


def test_platform_managed_connection_ignores_tenant_api_base(db: Session) -> None:
    """租户夹带的服务地址不能覆盖平台配置，新草稿只保存平台托管地址。"""

    tenant = _seed_paid_tenant(db)
    db.add(
        PlatformProtocolService(
            channel_type="wechat_ipad",
            api_base="http://ipad.platform.internal:9064",
            is_enabled=True,
        )
    )
    db.commit()

    with tenant_scope(tenant.id):
        draft = begin_account_connection(
            db,
            channel_type="wechat_ipad",
            display_name="平台托管账号",
            connection_values={"api_base": "http://forged.internal:9064"},
        )

    assert draft.api_base == "http://ipad.platform.internal:9064"
    assert draft.api_base != "http://forged.internal:9064"
