from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import OpenApiApp
from app.models.saas import Tenant
from app.services.security import decrypt_secret, encrypt_secret


OPEN_API_TIMESTAMP_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class OpenApiAuthResult:
    """开放接口认证阶段返回的最小可信身份，不把 ORM 对象带入业务会话。"""

    tenant_id: int
    app_id: int


class OpenApiAuthError(ValueError):
    """开放接口鉴权异常。

    统一用 code 表示失败原因，路由层可以直接映射为 HTTP detail。
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def hash_open_api_secret(secret: str) -> str:
    """生成 app_secret 哈希。

    哈希只用于审计和后台比对，真实请求签名仍使用可解密密文还原后的 secret。
    """

    return hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()


def set_open_api_secret(app: OpenApiApp, secret: str) -> None:
    """保存开放接口密钥。

    HMAC 校验需要服务端还原密钥，因此这里同时保存密文和哈希；
    哈希便于确认密钥是否变更，密文受系统 secret_key 保护。
    """

    normalized = str(secret or "").strip()
    app.app_secret_encrypted = encrypt_secret(normalized)
    app.app_secret_hash = hash_open_api_secret(normalized) if normalized else None


def sign_open_api_request(secret: str, raw_body: bytes, timestamp: str) -> str:
    """按开放接口规范生成请求签名。"""

    message = str(timestamp or "").encode("utf-8") + b"\n" + raw_body
    return hmac.new(str(secret or "").encode("utf-8"), message, hashlib.sha256).hexdigest()


def authenticate_open_api_request(db: Session, *, headers: dict[str, str], raw_body: bytes) -> OpenApiAuthResult:
    """校验开放接口请求。

    必填头：X-App-Key、X-Timestamp、X-Signature。X-Tenant-Code 用于共享数据库
    中精确定位租户；旧客户端未发送时只兼容全库唯一的 app_key。
    时间戳窗口限制可以降低抓包重放风险；业务幂等仍由 request_id 处理。
    """

    normalized_headers = {str(key).lower(): str(value or "") for key, value in headers.items()}
    app_key = normalized_headers.get("x-app-key", "").strip()
    tenant_code = normalized_headers.get("x-tenant-code", "").strip()
    timestamp = normalized_headers.get("x-timestamp", "").strip()
    signature = normalized_headers.get("x-signature", "").strip()
    if not app_key:
        raise OpenApiAuthError("missing_app_key")
    if not timestamp:
        raise OpenApiAuthError("missing_timestamp")
    if not signature:
        raise OpenApiAuthError("missing_signature")

    try:
        timestamp_value = int(timestamp)
    except ValueError as exc:
        raise OpenApiAuthError("invalid_timestamp") from exc
    if abs(int(time.time()) - timestamp_value) > OPEN_API_TIMESTAMP_TOLERANCE_SECONDS:
        raise OpenApiAuthError("timestamp_expired")

    stmt = select(OpenApiApp).where(OpenApiApp.app_key == app_key)
    if tenant_code:
        stmt = stmt.join(Tenant, Tenant.id == OpenApiApp.tenant_id).where(Tenant.code == tenant_code)
    apps = db.scalars(stmt.order_by(OpenApiApp.id.asc()).limit(2)).all()
    if not apps:
        raise OpenApiAuthError("app_not_found")
    if len(apps) > 1:
        # app_key 只在租户内唯一。未提供租户编码且出现重名时必须拒绝，不能按
        # 查询顺序任选一个租户，否则相同签名密钥配置会造成身份串用。
        raise OpenApiAuthError("tenant_code_required")
    app = apps[0]
    if str(app.status or "").strip() != "active":
        raise OpenApiAuthError("app_disabled")
    secret = decrypt_secret(app.app_secret_encrypted)
    if not secret:
        raise OpenApiAuthError("app_secret_not_configured")
    expected = sign_open_api_request(secret, raw_body, timestamp)
    if not hmac.compare_digest(expected, signature):
        raise OpenApiAuthError("invalid_signature")
    return OpenApiAuthResult(tenant_id=int(app.tenant_id), app_id=int(app.id))
