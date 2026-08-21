from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.core.time import ensure_shanghai_datetime, now_shanghai
from app.models.saas import (
    PLATFORM_ROLES,
    TENANT_ROLES,
    PlatformSession,
    PlatformUser,
    TenantSession,
    TenantSubscription,
    TenantUser,
)


# 参数使用 Argon2id 的交互式登录推荐量级。密码验证只发生在登录或敏感操作，
# 这里优先保证离线破解成本，不复用旧系统的快速 SHA-256 密码摘要。
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)
TENANT_ROLE_ORDER = {"readonly": 1, "operator": 2, "tenant_admin": 3}
SESSION_TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class CreatedSession:
    """会话创建结果；两个原始令牌只返回给调用方，不写入数据库或日志。"""

    record_id: int
    raw_token: str
    raw_csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class TenantPrincipal:
    """从有效数据库会话恢复出的可信租户员工身份。"""

    session_id: int
    tenant_id: int
    user_id: int
    username: str
    display_name: str
    role: str
    must_change_password: bool
    tenant_status: str
    subscription_status: str | None
    subscription_expires_at: datetime | None
    is_owner: bool = False


@dataclass(frozen=True)
class PlatformPrincipal:
    """从有效数据库会话恢复出的平台运营身份。"""

    session_id: int
    user_id: int
    username: str
    display_name: str
    role: str
    must_change_password: bool


def hash_password(password: str) -> str:
    """使用 Argon2id 生成带随机盐的密码哈希。"""

    if not password:
        raise ValueError("password_required")
    return PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """验证 Argon2id 密码，任何畸形哈希都统一视为认证失败。"""

    if not password or not password_hash:
        return False
    try:
        return bool(PASSWORD_HASHER.verify(password_hash, password))
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


def authenticate_platform_user(db: Session, username: str, password: str) -> PlatformUser | None:
    """只在平台账号表内验证凭据，租户员工即使同名也不能登录平台入口。"""

    normalized_username = (username or "").strip()
    if not normalized_username or not password:
        return None
    user = db.scalar(
        select(PlatformUser).where(
            PlatformUser.username == normalized_username,
            PlatformUser.is_enabled.is_(True),
        )
    )
    if user is None or user.role not in PLATFORM_ROLES:
        return None
    return user if verify_password(password, user.password_hash) else None


def authenticate_tenant_account(db: Session, mobile: str, password: str) -> TenantUser | None:
    """使用全局唯一手机号验证任意启用的企业账号，并恢复其可信租户归属。"""

    # 延迟导入避免 registration 在复用 hash_password 时形成模块初始化环；手机号规则只有一份实现。
    from app.services.registration import RegistrationError, normalize_mobile

    if not password:
        return None
    try:
        normalized_mobile = normalize_mobile(mobile)
    except RegistrationError:
        return None
    user = db.scalar(
        select(TenantUser).where(
            TenantUser.mobile == normalized_mobile,
            TenantUser.is_enabled.is_(True),
        )
    )
    if user is None or user.role not in TENANT_ROLES:
        return None
    return user if verify_password(password, user.password_hash) else None


def generate_initial_password() -> str:
    """生成一次性初始密码；调用方负责仅展示一次并立即丢弃原文。"""

    # 固定前缀确保至少包含大小写、数字和符号，随机主体提供足够熵。
    return f"Ss9!{secrets.token_urlsafe(18)}"


def hash_credential(raw_value: str) -> str:
    """为高熵会话、激活码和设备密钥生成固定长度 SHA-256 摘要。"""

    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def credential_matches(raw_value: str, expected_hash: str) -> bool:
    """用恒定时间比较高熵凭证摘要，避免泄露匹配前缀。"""

    if not raw_value or not expected_hash:
        return False
    return hmac.compare_digest(hash_credential(raw_value), expected_hash)


def normalize_tenant_role(value: str | None, default: str = "readonly") -> str:
    """把外部角色值收敛到首期固定角色集合。"""

    normalized = (value or default).strip().lower() or default
    return normalized if normalized in TENANT_ROLES else default


def has_tenant_role(principal: TenantPrincipal | None, required_role: str) -> bool:
    """按固定角色层级判断租户员工是否具备后端操作权限。"""

    # 权限名拼写错误或未来未显式接入的角色必须默认拒绝，不能按最低等级放行。
    if principal is None or required_role not in TENANT_ROLE_ORDER:
        return False
    return TENANT_ROLE_ORDER.get(principal.role, 0) >= TENANT_ROLE_ORDER[required_role]


def _session_expiry(expires_at: datetime | None) -> datetime:
    """计算会话绝对过期时间，并拒绝创建已经过期的会话。"""

    current = now_shanghai()
    resolved = ensure_shanghai_datetime(expires_at) if expires_at is not None else current + timedelta(
        seconds=max(300, settings.auth_cookie_max_age_seconds)
    )
    if resolved <= current:
        raise ValueError("session_expiry_invalid")
    return resolved


def _new_raw_token() -> str:
    """生成适合放入 Cookie 的高熵 URL-safe 随机令牌。"""

    return secrets.token_urlsafe(48)


def create_tenant_session(
    db: Session,
    user: TenantUser,
    *,
    expires_at: datetime | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> CreatedSession:
    """创建租户员工会话，并把可信 tenant_id 与认证版本固化到数据库。"""

    if user.id is None or user.tenant_id is None:
        raise ValueError("tenant_user_not_persisted")
    if user.role not in TENANT_ROLES:
        raise ValueError("tenant_role_invalid")

    raw_token = _new_raw_token()
    raw_csrf_token = _new_raw_token()
    resolved_expiry = _session_expiry(expires_at)
    record = TenantSession(
        tenant_id=user.tenant_id,
        tenant_user_id=user.id,
        session_token_hash=hash_credential(raw_token),
        csrf_token_hash=hash_credential(raw_csrf_token),
        auth_version=user.auth_version,
        expires_at=resolved_expiry,
        last_seen_at=now_shanghai(),
        ip_address=(ip_address or "").strip() or None,
        user_agent=(user_agent or "").strip() or None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return CreatedSession(record.id, raw_token, raw_csrf_token, resolved_expiry)


def create_platform_session(
    db: Session,
    user: PlatformUser,
    *,
    expires_at: datetime | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> CreatedSession:
    """创建平台账号会话，使用与租户完全独立的会话表和令牌链路。"""

    if user.id is None:
        raise ValueError("platform_user_not_persisted")
    if user.role not in PLATFORM_ROLES:
        raise ValueError("platform_role_invalid")

    raw_token = _new_raw_token()
    raw_csrf_token = _new_raw_token()
    resolved_expiry = _session_expiry(expires_at)
    record = PlatformSession(
        platform_user_id=user.id,
        session_token_hash=hash_credential(raw_token),
        csrf_token_hash=hash_credential(raw_csrf_token),
        auth_version=user.auth_version,
        expires_at=resolved_expiry,
        last_seen_at=now_shanghai(),
        ip_address=(ip_address or "").strip() or None,
        user_agent=(user_agent or "").strip() or None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return CreatedSession(record.id, raw_token, raw_csrf_token, resolved_expiry)


def _touch_session(db: Session, record: TenantSession | PlatformSession, current: datetime) -> None:
    """低频更新最近使用时间，保留活跃度同时避免每个请求都产生写入。"""

    last_seen = ensure_shanghai_datetime(record.last_seen_at)
    if current - last_seen < SESSION_TOUCH_INTERVAL:
        return
    record.last_seen_at = current
    db.commit()


def load_tenant_principal(db: Session, raw_token: str | None, *, touch: bool = True) -> TenantPrincipal | None:
    """校验会话、员工、租户和认证版本后恢复可信租户身份。"""

    if not raw_token:
        return None
    token_hash = hash_credential(raw_token)
    record = db.scalar(select(TenantSession).where(TenantSession.session_token_hash == token_hash))
    if record is None or not hmac.compare_digest(record.session_token_hash, token_hash):
        return None

    current = now_shanghai()
    user = record.user
    tenant = record.tenant
    if record.revoked_at is not None or ensure_shanghai_datetime(record.expires_at) <= current:
        return None
    if user is None or tenant is None or user.tenant_id != record.tenant_id:
        return None
    if tenant.status not in {"active", "disabled"} or not user.is_enabled or user.role not in TENANT_ROLES:
        return None
    if user.auth_version != record.auth_version:
        return None

    if touch:
        _touch_session(db, record, current)
    subscription = db.scalar(
        select(TenantSubscription)
        .where(TenantSubscription.tenant_id == tenant.id)
        .order_by(TenantSubscription.starts_at.desc(), TenantSubscription.id.desc())
        .limit(1)
    )
    return TenantPrincipal(
        session_id=record.id,
        tenant_id=record.tenant_id,
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        must_change_password=user.must_change_password,
        tenant_status=tenant.status,
        subscription_status=subscription.status if subscription else None,
        subscription_expires_at=subscription.expires_at if subscription else None,
        is_owner=bool(user.is_owner),
    )


def load_platform_principal(db: Session, raw_token: str | None, *, touch: bool = True) -> PlatformPrincipal | None:
    """校验平台会话和认证版本，不查询或接受任何租户员工身份。"""

    if not raw_token:
        return None
    token_hash = hash_credential(raw_token)
    record = db.scalar(select(PlatformSession).where(PlatformSession.session_token_hash == token_hash))
    if record is None or not hmac.compare_digest(record.session_token_hash, token_hash):
        return None

    current = now_shanghai()
    user = record.user
    if record.revoked_at is not None or ensure_shanghai_datetime(record.expires_at) <= current:
        return None
    if user is None or not user.is_enabled or user.role not in PLATFORM_ROLES:
        return None
    if user.auth_version != record.auth_version:
        return None

    if touch:
        _touch_session(db, record, current)
    return PlatformPrincipal(
        session_id=record.id,
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        must_change_password=user.must_change_password,
    )


def _clean_revoke_reason(reason: str) -> str:
    """限制吊销原因长度，避免异常输入污染审计字段。"""

    return (reason or "revoked").strip()[:255] or "revoked"


def revoke_tenant_session_by_token(db: Session, raw_token: str | None, *, reason: str = "logout") -> bool:
    """只吊销当前浏览器持有的租户会话，其他设备会话保持有效。"""

    if not raw_token:
        return False
    token_hash = hash_credential(raw_token)
    record = db.scalar(select(TenantSession).where(TenantSession.session_token_hash == token_hash))
    if record is None or not hmac.compare_digest(record.session_token_hash, token_hash):
        return False
    if record.revoked_at is None:
        record.revoked_at = now_shanghai()
        record.revoke_reason = _clean_revoke_reason(reason)
        db.commit()
    return True


def revoke_platform_session_by_token(db: Session, raw_token: str | None, *, reason: str = "logout") -> bool:
    """只吊销当前平台浏览器会话，不影响管理员在其他设备上的会话。"""

    if not raw_token:
        return False
    token_hash = hash_credential(raw_token)
    record = db.scalar(select(PlatformSession).where(PlatformSession.session_token_hash == token_hash))
    if record is None or not hmac.compare_digest(record.session_token_hash, token_hash):
        return False
    if record.revoked_at is None:
        record.revoked_at = now_shanghai()
        record.revoke_reason = _clean_revoke_reason(reason)
        db.commit()
    return True


def revoke_tenant_sessions(db: Session, user_id: int, *, reason: str = "revoked") -> int:
    """原子吊销租户员工当前全部有效会话。"""

    result = db.execute(
        update(TenantSession)
        .where(TenantSession.tenant_user_id == user_id, TenantSession.revoked_at.is_(None))
        .values(revoked_at=now_shanghai(), revoke_reason=_clean_revoke_reason(reason))
    )
    db.commit()
    return int(result.rowcount or 0)


def revoke_platform_sessions(db: Session, user_id: int, *, reason: str = "revoked") -> int:
    """原子吊销平台账号当前全部有效会话。"""

    result = db.execute(
        update(PlatformSession)
        .where(PlatformSession.platform_user_id == user_id, PlatformSession.revoked_at.is_(None))
        .values(revoked_at=now_shanghai(), revoke_reason=_clean_revoke_reason(reason))
    )
    db.commit()
    return int(result.rowcount or 0)
