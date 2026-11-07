"""Provider 节点额度、激活码、设备凭证和停用服务。"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.entities import Channel
from app.models.saas import (
    ProviderActivationCode,
    ProviderCredential,
    ProviderHeartbeat,
    ProviderNode,
    ProviderNodeAccount,
    SaasPlan,
    Tenant,
    TenantSubscription,
)
from app.services.channel_catalog import get_channel_type_definition
from app.services.saas_auth import credential_matches, hash_credential


class ProviderNodeError(ValueError):
    """Provider 节点业务规则不满足。"""


class ProviderNodeLimitReached(ProviderNodeError):
    """当前套餐的节点额度已经占满。"""


class ActivationCodeInvalid(ProviderNodeError):
    """激活码不存在、过期、已使用或节点不可激活。"""


class DeviceCredentialInvalid(ProviderNodeError):
    """设备凭证不存在、已吊销或节点已停用。"""


@dataclass(frozen=True)
class IssuedActivationCode:
    node_id: int
    raw_code: str
    expires_at: object


@dataclass(frozen=True)
class ActivatedProviderNode:
    tenant_id: int
    node_id: int
    credential_id: int
    device_id: str
    raw_device_token: str


@dataclass(frozen=True)
class DeviceCredentialIdentity:
    tenant_id: int
    node_id: int
    credential_id: int
    device_id: str


def _current_subscription(db: Session, tenant_id: int, *, for_update: bool = False) -> TenantSubscription | None:
    """读取最新订阅；创建节点时锁定订阅行，串行化同租户额度判断。"""

    statement = (
        select(TenantSubscription)
        .where(TenantSubscription.tenant_id == int(tenant_id))
        .order_by(TenantSubscription.starts_at.desc(), TenantSubscription.id.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def get_provider_node_quota(db: Session, tenant_id: int | None = None) -> dict[str, int]:
    """返回当前租户节点额度及占用量，待激活节点同样占用额度。"""

    trusted_tenant_id = require_tenant_id()
    resolved_tenant_id = int(tenant_id or trusted_tenant_id)
    if resolved_tenant_id != trusted_tenant_id:
        raise ProviderNodeError("tenant_scope_mismatch")
    subscription = _current_subscription(db, resolved_tenant_id)
    if subscription is None:
        raise ProviderNodeError("tenant_subscription_not_found")
    plan = db.get(SaasPlan, subscription.plan_id)
    if plan is None:
        raise ProviderNodeError("tenant_plan_not_found")
    used = int(
        db.scalar(
            select(func.count(ProviderNode.id)).where(
                ProviderNode.tenant_id == resolved_tenant_id,
                ProviderNode.status.in_(("pending", "active")),
            )
        )
        or 0
    )
    return {"limit": int(plan.provider_node_limit), "used": used, "remaining": max(0, int(plan.provider_node_limit) - used)}


def create_provider_node(
    db: Session,
    tenant_id: int,
    *,
    name: str,
    notes: str = "",
) -> ProviderNode:
    """创建待激活节点并立即占用额度。"""

    trusted_tenant_id = require_tenant_id()
    if int(tenant_id) != trusted_tenant_id:
        raise ProviderNodeError("tenant_scope_mismatch")
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise ProviderNodeError("provider_node_name_required")

    subscription = _current_subscription(db, trusted_tenant_id, for_update=True)
    if subscription is None or subscription.status != "active":
        raise ProviderNodeError("tenant_subscription_inactive")
    if subscription.expires_at and ensure_shanghai_datetime(subscription.expires_at) <= now_shanghai():
        raise ProviderNodeError("tenant_subscription_expired")
    plan = db.get(SaasPlan, subscription.plan_id)
    if plan is None or plan.status != "active":
        raise ProviderNodeError("tenant_plan_inactive")
    if db.scalar(
        select(ProviderNode.id).where(
            ProviderNode.tenant_id == trusted_tenant_id,
            ProviderNode.name == normalized_name,
        )
    ) is not None:
        raise ProviderNodeError("provider_node_name_exists")

    used = int(
        db.scalar(
            select(func.count(ProviderNode.id)).where(
                ProviderNode.tenant_id == trusted_tenant_id,
                ProviderNode.status.in_(("pending", "active")),
            )
        )
        or 0
    )
    if used >= int(plan.provider_node_limit):
        raise ProviderNodeLimitReached("provider_node_limit_reached")

    node = ProviderNode(
        tenant_id=trusted_tenant_id,
        name=normalized_name,
        status="pending",
        notes=(notes or "").strip() or None,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def list_provider_nodes(db: Session) -> list[ProviderNode]:
    """返回当前租户节点及发现账号，供管理页展示。"""

    tenant_id = require_tenant_id()
    return db.scalars(
        select(ProviderNode)
        .where(ProviderNode.tenant_id == tenant_id)
        .options(selectinload(ProviderNode.accounts))
        .order_by(ProviderNode.id.desc())
    ).all()


def get_provider_node(db: Session, node_id: int) -> ProviderNode | None:
    """按当前租户读取节点，其他租户主键统一返回不存在。"""

    tenant_id = require_tenant_id()
    return db.scalar(
        select(ProviderNode)
        .where(ProviderNode.id == int(node_id), ProviderNode.tenant_id == tenant_id)
        .options(selectinload(ProviderNode.accounts))
    )


def issue_activation_code(
    db: Session,
    node_id: int,
    *,
    created_by_user_id: int | None = None,
    ttl_minutes: int = 30,
) -> IssuedActivationCode:
    """签发一次性激活码；重新签发会立即废止同节点旧未使用码。"""

    node = get_provider_node(db, node_id)
    if node is None:
        raise ProviderNodeError("provider_node_not_found")
    if node.status == "disabled":
        raise ProviderNodeError("provider_node_disabled")
    if node.status == "active":
        # 已激活节点不能直接再签发第二套设备凭证；管理员必须走重新激活流程，
        # 先吊销旧密钥再生成新码，避免同一节点长期存在多把有效设备密钥。
        raise ProviderNodeError("provider_node_reactivation_required")

    current = now_shanghai()
    # 模型不额外增加 invalidated_at；重新签发时把旧码标记为已消费，验证阶段只需
    # 检查 consumed_at 即可统一拒绝旧码，不泄露是主动废止还是已经使用。
    db.execute(
        update(ProviderActivationCode)
        .where(
            ProviderActivationCode.tenant_id == node.tenant_id,
            ProviderActivationCode.provider_node_id == node.id,
            ProviderActivationCode.consumed_at.is_(None),
        )
        .values(consumed_at=current)
    )
    raw_code = f"pact_{secrets.token_urlsafe(32)}"
    expires_at = current + timedelta(minutes=max(1, int(ttl_minutes or 30)))
    record = ProviderActivationCode(
        tenant_id=node.tenant_id,
        provider_node_id=node.id,
        code_hash=hash_credential(raw_code),
        expires_at=expires_at,
        created_by_user_id=created_by_user_id,
    )
    db.add(record)
    db.commit()
    return IssuedActivationCode(node_id=int(node.id), raw_code=raw_code, expires_at=expires_at)


def activate_provider_node(
    db: Session,
    raw_code: str,
    *,
    fingerprint: str,
    client_version: str = "",
) -> ActivatedProviderNode:
    """消费激活码并签发长期设备密钥，调用方使用显式平台会话。"""

    normalized_code = (raw_code or "").strip()
    normalized_fingerprint = (fingerprint or "").strip()
    if not normalized_code or not normalized_fingerprint:
        raise ActivationCodeInvalid("activation_code_invalid")
    record = db.scalar(
        select(ProviderActivationCode)
        .where(ProviderActivationCode.code_hash == hash_credential(normalized_code))
        .with_for_update()
    )
    current = now_shanghai()
    if (
        record is None
        or record.consumed_at is not None
        or ensure_shanghai_datetime(record.expires_at) <= current
        or not credential_matches(normalized_code, record.code_hash)
    ):
        raise ActivationCodeInvalid("activation_code_invalid")
    node = db.get(ProviderNode, record.provider_node_id)
    if node is None or node.tenant_id != record.tenant_id or node.status == "disabled":
        raise ActivationCodeInvalid("activation_code_invalid")

    device_id = f"pnode_{secrets.token_urlsafe(18)}"
    raw_device_token = f"pdev_{secrets.token_urlsafe(48)}"
    credential = ProviderCredential(
        tenant_id=node.tenant_id,
        provider_node_id=node.id,
        token_hash=hash_credential(raw_device_token),
        device_id=device_id,
        fingerprint_hash=hashlib.sha256(normalized_fingerprint.encode("utf-8")).hexdigest(),
        client_version=(client_version or "").strip() or None,
        issued_at=current,
        last_used_at=current,
    )
    record.consumed_at = current
    node.status = "active"
    node.device_id = device_id
    node.client_version = credential.client_version
    node.activated_at = current
    node.disabled_at = None
    db.add(credential)
    db.commit()
    db.refresh(credential)
    return ActivatedProviderNode(
        tenant_id=int(node.tenant_id),
        node_id=int(node.id),
        credential_id=int(credential.id),
        device_id=device_id,
        raw_device_token=raw_device_token,
    )


def authenticate_device_token(db: Session, raw_device_token: str, *, touch: bool = True) -> DeviceCredentialIdentity:
    """把设备密钥收敛为可信租户、节点和凭证主键。"""

    normalized = (raw_device_token or "").strip()
    if not normalized:
        raise DeviceCredentialInvalid("device_token_required")
    credential = db.scalar(
        select(ProviderCredential).where(ProviderCredential.token_hash == hash_credential(normalized))
    )
    if credential is None or credential.revoked_at is not None or not credential_matches(normalized, credential.token_hash):
        raise DeviceCredentialInvalid("device_token_invalid")
    node = db.get(ProviderNode, credential.provider_node_id)
    if node is None or node.tenant_id != credential.tenant_id or node.status != "active":
        raise DeviceCredentialInvalid("device_token_invalid")
    if touch:
        credential.last_used_at = now_shanghai()
        db.commit()
    return DeviceCredentialIdentity(
        tenant_id=int(credential.tenant_id),
        node_id=int(credential.provider_node_id),
        credential_id=int(credential.id),
        device_id=credential.device_id,
    )


def disable_provider_node(db: Session, node_id: int) -> ProviderNode:
    """停用节点并在同一事务吊销所有设备凭证和未使用激活码。"""

    node = get_provider_node(db, node_id)
    if node is None:
        raise ProviderNodeError("provider_node_not_found")
    current = now_shanghai()
    node.status = "disabled"
    node.disabled_at = current
    db.execute(
        update(ProviderCredential)
        .where(
            ProviderCredential.tenant_id == node.tenant_id,
            ProviderCredential.provider_node_id == node.id,
            ProviderCredential.revoked_at.is_(None),
        )
        .values(revoked_at=current, revoke_reason="provider_node_disabled")
    )
    db.execute(
        update(ProviderActivationCode)
        .where(
            ProviderActivationCode.tenant_id == node.tenant_id,
            ProviderActivationCode.provider_node_id == node.id,
            ProviderActivationCode.consumed_at.is_(None),
        )
        .values(consumed_at=current)
    )
    db.commit()
    db.refresh(node)
    return node


def revoke_provider_node_credentials(
    db: Session,
    node_id: int,
    *,
    reason: str = "provider_node_revoked",
    allow_disabled: bool = False,
) -> ProviderNode:
    """吊销节点全部设备凭证并切回待激活状态，不自动生成或展示新激活码。"""

    node = get_provider_node(db, node_id)
    if node is None:
        raise ProviderNodeError("provider_node_not_found")
    if node.status == "disabled" and not allow_disabled:
        raise ProviderNodeError("provider_node_disabled")
    current = now_shanghai()
    db.execute(
        update(ProviderCredential)
        .where(
            ProviderCredential.tenant_id == node.tenant_id,
            ProviderCredential.provider_node_id == node.id,
            ProviderCredential.revoked_at.is_(None),
        )
        .values(revoked_at=current, revoke_reason=(reason or "provider_node_revoked")[:255])
    )
    db.execute(
        update(ProviderActivationCode)
        .where(
            ProviderActivationCode.tenant_id == node.tenant_id,
            ProviderActivationCode.provider_node_id == node.id,
            ProviderActivationCode.consumed_at.is_(None),
        )
        .values(consumed_at=current)
    )
    node.status = "pending"
    node.device_id = None
    node.client_version = None
    node.activated_at = None
    node.last_heartbeat_at = None
    node.disabled_at = None
    db.commit()
    db.refresh(node)
    return node


def reactivate_provider_node(
    db: Session,
    node_id: int,
    *,
    created_by_user_id: int | None = None,
    ttl_minutes: int = 30,
) -> IssuedActivationCode:
    """吊销旧设备密钥后签发一次性重新激活码，原文仍只返回当前调用一次。"""

    node = get_provider_node(db, node_id)
    if node is None:
        raise ProviderNodeError("provider_node_not_found")
    if node.status == "disabled" and get_provider_node_quota(db)["remaining"] <= 0:
        raise ProviderNodeLimitReached("provider_node_limit_reached")
    revoke_provider_node_credentials(
        db,
        node_id,
        reason="provider_node_reactivation",
        allow_disabled=True,
    )
    return issue_activation_code(
        db,
        node_id,
        created_by_user_id=created_by_user_id,
        ttl_minutes=ttl_minutes,
    )


def validate_channel_executor_binding(
    db: Session,
    *,
    channel_type: str,
    provider_node_id: int | None,
    provider_account_id: int | None,
    channel_id: int | None = None,
) -> tuple[ProviderNode | None, ProviderNodeAccount | None]:
    """按渠道能力校验执行器绑定；不需要远端执行器的渠道始终清空绑定字段。"""

    normalized_type = (channel_type or "").strip()
    node_id = int(provider_node_id or 0)
    account_id = int(provider_account_id or 0)
    definition = get_channel_type_definition(normalized_type)
    if not definition.requires_executor_binding:
        # 浏览器提交内容不构成可信绑定依据。直连、Webhook 和平台轮询渠道无论
        # 是否夹带节点主键，都必须在服务端归一为空，避免误占企业微信执行账号。
        return None, None
    if not node_id or not account_id:
        raise ProviderNodeError("channel_executor_binding_required")

    tenant_id = require_tenant_id()
    node = get_provider_node(db, node_id)
    if node is None:
        raise ProviderNodeError("provider_node_not_found")
    if node.status != "active":
        raise ProviderNodeError("provider_node_not_active")
    account = db.scalar(
        select(ProviderNodeAccount).where(
            ProviderNodeAccount.id == account_id,
            ProviderNodeAccount.tenant_id == tenant_id,
            ProviderNodeAccount.provider_node_id == node.id,
        )
    )
    if account is None:
        raise ProviderNodeError("provider_account_not_found")

    current_channel = db.get(Channel, int(channel_id)) if channel_id else None
    retaining_current_binding = bool(
        current_channel
        and int(current_channel.provider_node_id or 0) == node.id
        and int(current_channel.provider_account_id or 0) == account.id
    )
    if account.status != "online" and not retaining_current_binding:
        raise ProviderNodeError("provider_account_not_online")
    occupied = db.scalar(
        select(Channel.id).where(
            Channel.provider_account_id == account.id,
            Channel.id != int(channel_id or 0),
        )
    )
    if occupied is not None:
        # 入站接口只携带节点账号键；一个账号若绑定多个渠道将无法唯一解析目标渠道，
        # 因此即使另一个渠道已停用，也必须先显式解绑再重新分配。
        raise ProviderNodeError("provider_account_already_bound")
    return node, account


def provider_poll_allowed(db: Session, tenant_id: int) -> bool:
    """到期或停用租户仍可心跳，但不能领取新的 Provider 任务。"""

    tenant = db.get(Tenant, int(tenant_id))
    if tenant is None or tenant.status != "active":
        return False
    subscription = _current_subscription(db, tenant_id)
    if subscription is None:
        # 历史自营租户可能尚未补订阅；迁移完成前保留兼容，不影响正式租户策略。
        return True
    if subscription.status != "active":
        return False
    return not (
        subscription.expires_at
        and ensure_shanghai_datetime(subscription.expires_at) <= now_shanghai()
    )


def record_provider_heartbeat(
    db: Session,
    *,
    node_id: int,
    client_version: str = "",
    hook_status: str = "",
    workwechat_status: str = "",
    accounts: list[dict] | None = None,
    details: dict | None = None,
) -> ProviderNode:
    """更新节点在线状态并同步节点内发现账号，但不自动改变渠道绑定。"""

    node = get_provider_node(db, node_id)
    if node is None or node.status != "active":
        raise DeviceCredentialInvalid("provider_node_inactive")
    current = now_shanghai()
    normalized_accounts = [item for item in (accounts or []) if isinstance(item, dict)]
    node.client_version = (client_version or "").strip() or node.client_version
    node.last_heartbeat_at = current

    seen_keys: set[str] = set()
    for item in normalized_accounts:
        account_key = str(item.get("account_key") or "").strip()
        if not account_key or account_key in seen_keys:
            continue
        seen_keys.add(account_key)
        account = db.scalar(
            select(ProviderNodeAccount).where(
                ProviderNodeAccount.tenant_id == node.tenant_id,
                ProviderNodeAccount.provider_node_id == node.id,
                ProviderNodeAccount.account_key == account_key,
            )
        )
        if account is None:
            account = ProviderNodeAccount(
                tenant_id=node.tenant_id,
                provider_node_id=node.id,
                account_key=account_key,
            )
            db.add(account)
        account.display_name = str(item.get("display_name") or item.get("name") or "").strip() or None
        account.external_user_id = str(item.get("external_user_id") or item.get("userid") or "").strip() or None
        reported_status = str(item.get("status") or "unknown").strip().lower()
        account.status = reported_status if reported_status in {"online", "offline", "unknown"} else "unknown"
        account.last_seen_at = current
        account.metadata_json = json.dumps(item, ensure_ascii=False, sort_keys=True)

    # 本次心跳没有上报的旧账号只标记离线，不删除记录，确保渠道绑定主键稳定。
    for account in db.scalars(
        select(ProviderNodeAccount).where(
            ProviderNodeAccount.tenant_id == node.tenant_id,
            ProviderNodeAccount.provider_node_id == node.id,
        )
    ).all():
        if account.account_key not in seen_keys:
            account.status = "offline"

    db.add(
        ProviderHeartbeat(
            tenant_id=node.tenant_id,
            provider_node_id=node.id,
            reported_at=current,
            client_version=node.client_version,
            hook_status=(hook_status or "").strip() or None,
            workwechat_status=(workwechat_status or "").strip() or None,
            accounts_json=json.dumps(normalized_accounts, ensure_ascii=False, sort_keys=True),
            details_json=json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
        )
    )
    db.commit()
    db.refresh(node)
    return node
