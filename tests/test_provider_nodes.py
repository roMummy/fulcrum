from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.tenant_context import tenant_scope
from app.core.tenant_db import tenant_session
from app.core.time import now_shanghai
from app.models.saas import (
    ProviderActivationCode,
    ProviderCredential,
    SaasPlan,
    Tenant,
    TenantSubscription,
)
from app.services.provider_nodes import (
    ActivationCodeInvalid,
    DeviceCredentialInvalid,
    ProviderNodeLimitReached,
    activate_provider_node,
    authenticate_device_token,
    create_provider_node,
    disable_provider_node,
    get_provider_node_quota,
    issue_activation_code,
)
from app.services.saas_auth import hash_credential


@pytest.fixture()
def provider_context(tmp_path):
    """为节点额度与激活链路提供共享临时数据库。"""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider-nodes.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        tenant = Tenant(code="provider-test", name="Provider 测试租户", status="active")
        plan = SaasPlan(code="one-node", name="单节点套餐", provider_node_limit=1, status="active")
        db.add_all([tenant, plan])
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
        ids = {"tenant": int(tenant.id), "plan": int(plan.id)}
    try:
        yield factory, ids
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _create_node(factory, tenant_id: int, name: str = "客服电脑A"):
    with tenant_scope(tenant_id), tenant_session(factory) as db:
        return create_provider_node(db, tenant_id, name=name)


def test_provider_node_creation_reserves_quota_while_pending(provider_context) -> None:
    """待激活节点立即占用额度，不能通过重复创建绕过套餐限制。"""

    factory, ids = provider_context
    first = _create_node(factory, ids["tenant"])
    assert first.status == "pending"
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        assert get_provider_node_quota(db) == {"limit": 1, "used": 1, "remaining": 0}
        with pytest.raises(ProviderNodeLimitReached, match="provider_node_limit_reached"):
            create_provider_node(db, ids["tenant"], name="客服电脑B")


def test_activation_code_is_single_use_and_device_token_is_only_returned_once(provider_context) -> None:
    """激活码和设备 token 原文都不能出现在数据库字段中。"""

    factory, ids = provider_context
    node = _create_node(factory, ids["tenant"])
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        issued = issue_activation_code(db, node.id, ttl_minutes=30)

    with factory() as db:
        activated = activate_provider_node(
            db,
            issued.raw_code,
            fingerprint="device-fingerprint-a",
            client_version="1.0.0",
        )
        assert activated.raw_device_token.startswith("pdev_")
        assert authenticate_device_token(db, activated.raw_device_token).node_id == node.id
        code_record = db.scalar(select(ProviderActivationCode).where(ProviderActivationCode.provider_node_id == node.id))
        credential = db.get(ProviderCredential, activated.credential_id)
        assert code_record.code_hash == hash_credential(issued.raw_code)
        assert issued.raw_code not in code_record.code_hash
        assert credential.token_hash == hash_credential(activated.raw_device_token)
        assert activated.raw_device_token not in credential.token_hash
        with pytest.raises(ActivationCodeInvalid, match="activation_code_invalid"):
            activate_provider_node(db, issued.raw_code, fingerprint="device-fingerprint-a")


def test_disabling_node_revokes_credentials_and_releases_quota(provider_context) -> None:
    """停用节点立即使设备凭证失效，并允许租户创建替代节点。"""

    factory, ids = provider_context
    node = _create_node(factory, ids["tenant"])
    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        issued = issue_activation_code(db, node.id)
    with factory() as db:
        activated = activate_provider_node(db, issued.raw_code, fingerprint="device-a")

    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        disabled = disable_provider_node(db, node.id)
        assert disabled.status == "disabled"
        assert get_provider_node_quota(db)["remaining"] == 1
        replacement = create_provider_node(db, ids["tenant"], name="客服电脑B")
        assert replacement.status == "pending"

    with factory() as db:
        with pytest.raises(DeviceCredentialInvalid, match="device_token_invalid"):
            authenticate_device_token(db, activated.raw_device_token)


def test_lowering_plan_limit_keeps_existing_node_but_blocks_new_nodes(provider_context) -> None:
    """降低额度不自动停用已有节点，但后续新增必须按新额度拒绝。"""

    factory, ids = provider_context
    node = _create_node(factory, ids["tenant"])
    with factory() as db:
        plan = db.get(SaasPlan, ids["plan"])
        plan.provider_node_limit = 0
        db.commit()

    with tenant_scope(ids["tenant"]), tenant_session(factory) as db:
        assert db.get(type(node), node.id).status == "pending"
        with pytest.raises(ProviderNodeLimitReached):
            create_provider_node(db, ids["tenant"], name="超额节点")
