"""平台托管协议服务的保存、查询和租户接入解析能力。"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.models.saas import PlatformAuditLog, PlatformProtocolService
from app.services.channel_catalog import get_channel_type_definition, list_channel_type_definitions


class PlatformProtocolServiceError(ValueError):
    """平台协议服务缺失、停用或配置非法时使用的稳定业务错误。"""


def _normalize_api_base(value: str) -> str:
    """规范化平台服务地址，只接受带主机的 HTTP 或 HTTPS 根地址。"""

    normalized = str(value or "").strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PlatformProtocolServiceError("platform_protocol_service_url_invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        # 账号密码、查询参数和片段都不属于稳定服务根地址，避免凭证混入普通 URL 字段。
        raise PlatformProtocolServiceError("platform_protocol_service_url_invalid")
    return normalized


def _require_managed_definition(channel_type: str):
    """只允许渠道目录明确声明的平台托管类型进入本服务。"""

    definition = get_channel_type_definition(channel_type)
    if not definition.platform_managed_endpoint:
        raise PlatformProtocolServiceError("platform_protocol_service_type_not_managed")
    return definition


def get_platform_protocol_service(db: Session, channel_type: str) -> PlatformProtocolService | None:
    """按稳定渠道编码读取平台配置，不使用租户上下文或 Provider 节点。"""

    normalized_type = _require_managed_definition(channel_type).code
    return db.scalar(
        select(PlatformProtocolService).where(PlatformProtocolService.channel_type == normalized_type)
    )


def require_enabled_platform_protocol_service(db: Session, channel_type: str) -> PlatformProtocolService:
    """返回允许新接入的平台服务，缺失和停用必须显式失败。"""

    service = get_platform_protocol_service(db, channel_type)
    if service is None:
        raise PlatformProtocolServiceError("platform_protocol_service_missing")
    if not service.is_enabled:
        raise PlatformProtocolServiceError("platform_protocol_service_disabled")
    return service


def list_platform_protocol_services(db: Session) -> list[dict[str, object]]:
    """按渠道目录顺序输出平台托管服务及其已同步渠道数量。"""

    rows: list[dict[str, object]] = []
    for definition in list_channel_type_definitions():
        if not definition.platform_managed_endpoint:
            continue
        service = db.scalar(
            select(PlatformProtocolService).where(
                PlatformProtocolService.channel_type == definition.code
            )
        )
        channel_count = db.scalar(
            select(func.count(Channel.id)).where(Channel.channel_type == definition.code, Channel.deleted_at.is_(None))
        ) or 0
        rows.append(
            {
                "channel_type": definition.code,
                "label": definition.label,
                "summary": definition.summary,
                "api_base": service.api_base if service is not None else "",
                "is_enabled": bool(service and service.is_enabled),
                "is_configured": service is not None,
                "channel_count": int(channel_count),
            }
        )
    return rows


def save_platform_protocol_service(
    db: Session,
    *,
    channel_type: str,
    api_base: str,
    is_enabled: bool,
    actor_user_id: int | None,
) -> PlatformProtocolService:
    """保存平台服务并同步所有同类型渠道，保证平台地址是唯一写入来源。"""

    definition = _require_managed_definition(channel_type)
    normalized_api_base = _normalize_api_base(api_base)
    service = db.scalar(
        select(PlatformProtocolService).where(
            PlatformProtocolService.channel_type == definition.code
        )
    )
    if service is None:
        service = PlatformProtocolService(
            channel_type=definition.code,
            api_base=normalized_api_base,
            is_enabled=bool(is_enabled),
        )
        db.add(service)
    else:
        service.api_base = normalized_api_base
        service.is_enabled = bool(is_enabled)
    db.flush()

    # 已有渠道继续保存实际运行地址；平台每次保存都会统一覆盖，租户页面没有反向写入入口。
    channel_count = db.scalar(
        select(func.count(Channel.id)).where(Channel.channel_type == definition.code)
    ) or 0
    db.execute(
        update(Channel)
        .where(Channel.channel_type == definition.code)
        .values(api_base=normalized_api_base)
    )
    db.add(
        PlatformAuditLog(
            actor_user_id=actor_user_id,
            action="protocol_service.save",
            target_type="platform_protocol_service",
            target_id=service.id,
            detail=(
                f"channel_type={definition.code};is_enabled={str(bool(is_enabled)).lower()};"
                f"synchronized_channels={int(channel_count)}"
            ),
        )
    )
    db.commit()
    db.refresh(service)
    return service
