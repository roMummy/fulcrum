from __future__ import annotations

import base64
import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.models.entities import AdminUser


AUTH_COOKIE_NAME = "mba_auth"
ROLE_ORDER = {"readonly": 1, "ops": 2, "admin": 3}
ALLOWED_ROLES = set(ROLE_ORDER)


def hash_password(password: str) -> str:
    return hash_password_with_secret(settings.secret_key, password)


def hash_password_with_secret(secret: str, password: str) -> str:
    return hashlib.sha256(f"{secret}:{password}".encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password), password_hash)


def build_auth_cookie(user: AdminUser) -> str:
    payload = f"{user.username}:{user.role}:{user.auth_version}"
    signature = hmac.new(settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode("utf-8")).decode("ascii")


def parse_auth_cookie(value: str | None) -> tuple[str, str, int] | None:
    if not value:
        return None
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        username, role, auth_version_raw, signature = decoded.split(":", 3)
        auth_version = int(auth_version_raw)
    except Exception:
        return None

    payload = f"{username}:{role}:{auth_version}"
    expected = hmac.new(settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    return username, role, auth_version


def get_user_by_cookie(db: Session, value: str | None) -> AdminUser | None:
    parsed = parse_auth_cookie(value)
    if not parsed:
        return None
    username, role, auth_version = parsed
    user = db.scalar(select(AdminUser).where(AdminUser.username == username, AdminUser.is_enabled.is_(True)))
    if not user or user.role != role or user.auth_version != auth_version:
        return None
    return user


def has_required_role(user: AdminUser | None, required_role: str) -> bool:
    if not user:
        return False
    return ROLE_ORDER.get(user.role, 0) >= ROLE_ORDER.get(required_role, 0)


def normalize_role(value: str | None, default: str = "readonly") -> str:
    normalized = (value or default).strip().lower() or default
    return normalized if normalized in ALLOWED_ROLES else default


def validate_password_change(password: str) -> str | None:
    cleaned = password.strip()
    if len(cleaned) < 6:
        return "密码至少需要 6 位"
    if len(cleaned) > 128:
        return "密码长度不能超过 128 位"
    return None
