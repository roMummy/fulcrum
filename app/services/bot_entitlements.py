"""通用机器人额度、设备归属和试用防滥用服务。"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.settings import settings
from app.core.tenant_context import require_tenant_id
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import BotBinding, DeviceTrialClaim, PlatformAuditLog, PlatformUser
from app.services.billing import get_current_subscription


ADAPTER_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
DEVICE_FINGERPRINT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BotEntitlementError(ValueError):
    """订阅额度、账号绑定或设备防滥用规则不满足。"""


@dataclass(frozen=True)
class BotQuota:
    """当前租户可信订阅对应的机器人额度快照。"""

    total: int
    used: int
    remaining: int
    plan_code: str
    expires_at: datetime | None
    is_trial: bool


def normalize_adapter_type(value: str) -> str:
    """规范化适配类型，保持核心服务与具体 Provider 类名解耦。"""

    normalized = (value or "").strip().lower()
    if not ADAPTER_TYPE_PATTERN.fullmatch(normalized):
        raise BotEntitlementError("adapter_type_invalid")
    return normalized


def normalize_bot_account_id(value: str) -> str:
    """规范化外部机器人账号，保留可能有意义的大小写但拒绝控制字符。"""

    normalized = unicodedata.normalize("NFKC", value or "").strip()
    if not normalized or len(normalized) > 191:
        raise BotEntitlementError("bot_account_id_invalid")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise BotEntitlementError("bot_account_id_invalid")
    return normalized


def hash_device_fingerprint(raw_fingerprint: str) -> str:
    """用 SHA-256 HMAC 生成全平台稳定摘要，原始设备指纹绝不离开当前调用栈。"""

    normalized = (raw_fingerprint or "").strip()
    if len(normalized) < 8 or len(normalized) > 512:
        raise BotEntitlementError("device_fingerprint_invalid")
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        f"bot-device:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def normalize_device_fingerprint_hash(value: str) -> str:
    """校验只允许内部连接服务传入的固定长度设备摘要。"""

    normalized = str(value or "").strip().lower()
    if not DEVICE_FINGERPRINT_HASH_PATTERN.fullmatch(normalized):
        raise BotEntitlementError("device_fingerprint_hash_invalid")
    return normalized


def get_bot_quota(db: Session) -> BotQuota:
    """从可信租户上下文和当前有效订阅计算已用、总量与剩余额度。"""

    tenant_id = require_tenant_id()
    subscription = get_current_subscription(db, tenant_id=tenant_id)
    if subscription is None or subscription.status != "active":
        raise BotEntitlementError("subscription_inactive")
    current_time = now_shanghai()
    if subscription.expires_at is not None and ensure_shanghai_datetime(subscription.expires_at) <= current_time:
        raise BotEntitlementError("subscription_expired")
    plan = subscription.plan
    if plan is None:
        raise BotEntitlementError("subscription_plan_missing")
    # 付费订单固化的额度优先于后来被平台修改的套餐值，保护已购买权益。
    total = (
        int(subscription.fulfilled_order.bot_limit_snapshot)
        if subscription.fulfilled_order is not None
        else int(plan.bot_limit)
    )
    if total <= 0:
        raise BotEntitlementError("subscription_bot_limit_invalid")
    used = int(
        db.scalar(
            select(func.count(BotBinding.id)).where(
                BotBinding.tenant_id == tenant_id,
                BotBinding.status.in_(("active", "disabled")),
            )
        )
        or 0
    )
    is_trial = bool(
        plan.code == "trial"
        or (plan.trial_days > 0 and plan.monthly_price_cents == 0 and plan.yearly_price_cents == 0)
    )
    return BotQuota(
        total=total,
        used=used,
        remaining=max(total - used, 0),
        plan_code=plan.code,
        expires_at=subscription.expires_at,
        is_trial=is_trial,
    )


def list_bot_bindings(db: Session) -> list[BotBinding]:
    """只返回可信租户上下文中的绑定记录，包括平台释放后的历史。"""

    tenant_id = require_tenant_id()
    return db.scalars(
        select(BotBinding)
        .where(BotBinding.tenant_id == tenant_id)
        .order_by(BotBinding.id.desc())
    ).all()


def add_verified_bot_binding(
    db: Session,
    *,
    adapter_type: str,
    bot_account_id: str,
    device_fingerprint_hash: str,
    display_name: str = "",
    bot_id: int | None = None,
    channel_id: int | None = None,
) -> BotBinding:
    """把已验证账号加入当前事务，并执行额度、设备和试用全局约束。"""

    tenant_id = require_tenant_id()
    quota = get_bot_quota(db)
    if quota.remaining <= 0:
        raise BotEntitlementError("bot_quota_exceeded")
    normalized_adapter = normalize_adapter_type(adapter_type)
    normalized_account = normalize_bot_account_id(bot_account_id)
    fingerprint_hash = normalize_device_fingerprint_hash(device_fingerprint_hash)
    normalized_display_name = (display_name or "").strip()
    if len(normalized_display_name) > 150:
        raise BotEntitlementError("display_name_too_long")

    same_account_bindings = db.scalars(
        select(BotBinding).where(
            BotBinding.adapter_type == normalized_adapter,
            BotBinding.bot_account_id == normalized_account,
        )
    ).all()
    if any(binding.status != "released" for binding in same_account_bindings):
        raise BotEntitlementError("bot_account_already_bound")
    # 数据库唯一约束 (adapter_type, bot_account_id) 决定同一账号只能有一条绑定记录，
    # 释放后重新接入必须复用原行，而不是插入新行。其他租户的历史绑定仍按已占用处理，
    # 避免释放操作把账号归属自动转移给别的租户。
    released_binding = same_account_bindings[0] if same_account_bindings else None
    if released_binding is not None and released_binding.tenant_id != tenant_id:
        raise BotEntitlementError("bot_account_already_bound")

    existing_device = db.scalar(
        select(BotBinding)
        .where(BotBinding.device_fingerprint_hash == fingerprint_hash)
        .order_by(BotBinding.id.asc())
        .limit(1)
    )
    if existing_device is not None and existing_device.tenant_id != tenant_id:
        raise BotEntitlementError("device_owned_by_other_tenant")

    existing_claim = db.scalar(
        select(DeviceTrialClaim).where(DeviceTrialClaim.device_fingerprint_hash == fingerprint_hash)
    )
    if quota.is_trial:
        # 同一试用租户在平台释放后也不能自行换号重领；领取记录永久保留。
        if existing_claim is not None:
            if existing_claim.tenant_id == tenant_id:
                raise BotEntitlementError("trial_device_already_claimed")
            raise BotEntitlementError("device_trial_already_claimed")
        db.add(DeviceTrialClaim(device_fingerprint_hash=fingerprint_hash, tenant_id=tenant_id))
    elif existing_claim is not None and existing_claim.tenant_id != tenant_id:
        # 付费不会绕过设备单租户归属；同一租户从试用续费后则继续复用原设备。
        raise BotEntitlementError("device_owned_by_other_tenant")

    if released_binding is not None:
        # 复用已释放的绑定行，清空释放信息并刷新为当前设备与渠道。
        released_binding.bot_id = bot_id
        released_binding.channel_id = channel_id
        released_binding.display_name = normalized_display_name or None
        released_binding.device_fingerprint_hash = fingerprint_hash
        released_binding.status = "active"
        released_binding.bound_at = now_shanghai()
        released_binding.released_at = None
        released_binding.released_by_user_id = None
        released_binding.release_reason = None
        try:
            db.flush()
            return released_binding
        except IntegrityError as exc:
            db.rollback()
            raise BotEntitlementError("bot_binding_conflict") from exc

    binding = BotBinding(
        tenant_id=tenant_id,
        bot_id=bot_id,
        channel_id=channel_id,
        adapter_type=normalized_adapter,
        bot_account_id=normalized_account,
        display_name=normalized_display_name or None,
        device_fingerprint_hash=fingerprint_hash,
        status="active",
        bound_at=now_shanghai(),
    )
    db.add(binding)
    try:
        # 这里只 flush，让账号接入服务可以把机器人、渠道、绑定和试用领取放进
        # 同一个事务。并发唯一冲突会回滚整个外层事务，避免留下半成品。
        db.flush()
        return binding
    except IntegrityError as exc:
        # 并发绑定由数据库唯一约束最终裁决，公开错误不包含账号或设备摘要。
        db.rollback()
        raise BotEntitlementError("bot_binding_conflict") from exc


def bind_bot_account(
    db: Session,
    *,
    adapter_type: str,
    bot_account_id: str,
    device_fingerprint: str,
    display_name: str = "",
) -> BotBinding:
    """为独立服务调用完成设备哈希、绑定写入和提交。"""

    binding = add_verified_bot_binding(
        db,
        adapter_type=adapter_type,
        bot_account_id=bot_account_id,
        device_fingerprint_hash=hash_device_fingerprint(device_fingerprint),
        display_name=display_name,
    )
    db.commit()
    db.refresh(binding)
    return binding


def list_all_bot_bindings(db: Session) -> list[BotBinding]:
    """供平台管理员查看全局绑定，租户业务不得调用此无范围查询。"""

    return db.scalars(
        select(BotBinding)
        .options(selectinload(BotBinding.tenant), selectinload(BotBinding.released_by_user))
        .order_by(BotBinding.id.desc())
    ).all()


def get_bot_binding_for_platform(db: Session, *, binding_id: int, for_update: bool = False) -> BotBinding:
    """平台按主键读取绑定，可选行锁保护受控释放。"""

    query = (
        select(BotBinding)
        .where(BotBinding.id == int(binding_id))
        .options(selectinload(BotBinding.tenant), selectinload(BotBinding.released_by_user))
    )
    if for_update:
        query = query.with_for_update()
    binding = db.scalar(query)
    if binding is None:
        raise BotEntitlementError("bot_binding_not_found")
    return binding


def release_bot_binding_by_platform(
    db: Session,
    *,
    binding_id: int,
    reviewer_user_id: int,
    reason: str,
) -> BotBinding:
    """由平台管理员受控释放绑定，保留原记录、设备摘要、原因和试用领取历史。"""

    try:
        binding = get_bot_binding_for_platform(db, binding_id=binding_id, for_update=True)
        if binding.status == "released":
            return binding
        reviewer = db.get(PlatformUser, int(reviewer_user_id))
        if reviewer is None or reviewer.role != "platform_admin" or not reviewer.is_enabled:
            raise BotEntitlementError("reviewer_not_allowed")
        normalized_reason = (reason or "").strip()
        if not normalized_reason:
            raise BotEntitlementError("release_reason_required")
        if len(normalized_reason) > 1000:
            raise BotEntitlementError("release_reason_too_long")
        current_time = now_shanghai()
        binding.status = "released"
        binding.released_at = current_time
        binding.released_by_user_id = reviewer.id
        binding.release_reason = normalized_reason
        db.add(
            PlatformAuditLog(
                actor_user_id=reviewer.id,
                action="bot_binding.release",
                target_type="bot_binding",
                target_id=binding.id,
                target_tenant_id=binding.tenant_id,
                # 只记录绑定主键、适配类型和原因；不记录设备原始指纹。
                detail=f"adapter_type={binding.adapter_type};reason={normalized_reason}",
            )
        )
        db.commit()
        db.refresh(binding)
        return binding
    except BaseException:
        db.rollback()
        raise
