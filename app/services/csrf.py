from __future__ import annotations

import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.saas import PlatformSession, TenantSession
from app.services.saas_auth import (
    credential_matches,
    hash_credential,
    load_platform_principal,
    load_tenant_principal,
)


def verify_csrf_token(
    db: Session,
    raw_session_token: str | None,
    cookie_token: str | None,
    form_token: str | None,
    *,
    platform: bool = False,
) -> bool:
    """验证双提交 CSRF，并确认令牌确实属于当前仍有效的数据库会话。"""

    if not raw_session_token or not cookie_token or not form_token:
        return False
    # Cookie 和表单令牌先做恒定时间比较，阻止攻击者只凭一侧值通过验证。
    # compare_digest 的字符串模式只接受 ASCII；外部表单可能包含任意 Unicode，
    # 统一编码为 UTF-8 字节后既保留恒定时间比较，也能把畸形输入稳定判定为失败。
    if not hmac.compare_digest(cookie_token.encode("utf-8"), form_token.encode("utf-8")):
        return False

    token_hash = hash_credential(raw_session_token)
    if platform:
        if load_platform_principal(db, raw_session_token, touch=False) is None:
            return False
        record = db.scalar(select(PlatformSession).where(PlatformSession.session_token_hash == token_hash))
    else:
        if load_tenant_principal(db, raw_session_token, touch=False) is None:
            return False
        record = db.scalar(select(TenantSession).where(TenantSession.session_token_hash == token_hash))

    if record is None or not hmac.compare_digest(record.session_token_hash, token_hash):
        return False
    return credential_matches(cookie_token, record.csrf_token_hash)
