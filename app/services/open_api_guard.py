"""开放 API 统一订阅、能力和额度门禁。"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, OpenApiApp, OpenApiAppChannel
from app.services.open_api_capabilities import get_open_api_capability
from app.services.open_api_quotas import OpenApiUsageReservation, reserve_open_api_usage


class OpenApiGuardError(ValueError):
    """开放接口请求被统一门禁拒绝时返回的业务错误。"""

    STATUS_CODES = {
        "subscription_required": 402,
        "quota_exceeded": 402,
        "app_disabled": 403,
        "channel_not_allowed": 403,
        "capability_not_supported": 403,
        "request_id_required": 400,
        "reservation_conflict": 409,
    }

    def __init__(self, code: str, *, metric: str = ""):
        super().__init__(code)
        self.code = code
        self.metric = metric
        self.status_code = self.STATUS_CODES.get(code, 403)


@dataclass(frozen=True)
class OpenApiGuardResult:
    """门禁通过后的额度预扣结果。"""

    request_id: str
    reservation: OpenApiUsageReservation


def guard_open_api_request(
    db: Session,
    *,
    app: OpenApiApp,
    request_id: str | None,
    usage: dict[str, int] | None = None,
    capability: str | None = None,
    channel_id: int | None = None,
) -> OpenApiGuardResult:
    """校验应用、订阅、能力、渠道授权并预扣本次请求额度。"""

    if str(app.status or "").strip() != "active":
        raise OpenApiGuardError("app_disabled")
    normalized_request_id = str(request_id or "").strip() or f"auto-{uuid.uuid4().hex}"

    channel = None
    if channel_id is not None:
        channel = db.scalar(
            select(Channel).where(
                Channel.id == int(channel_id),
                Channel.tenant_id == int(app.tenant_id),
                Channel.is_enabled.is_(True),
                Channel.deleted_at.is_(None),
            )
        )
        if channel is None:
            raise OpenApiGuardError("channel_not_allowed")
        authorized = db.scalar(
            select(OpenApiAppChannel.id).where(
                OpenApiAppChannel.app_id == int(app.id),
                OpenApiAppChannel.channel_id == int(channel.id),
                OpenApiAppChannel.tenant_id == int(app.tenant_id),
            )
        )
        if authorized is None:
            raise OpenApiGuardError("channel_not_allowed")

    if capability:
        item = get_open_api_capability(capability)
        if item is None or (channel is not None and channel.channel_type not in item.channel_types):
            raise OpenApiGuardError("capability_not_supported")

    try:
        reservation = reserve_open_api_usage(
            db,
            app=app,
            request_id=normalized_request_id,
            usage=usage or {"api_requests": 1},
        )
    except ValueError as exc:
        code = getattr(exc, "code", str(exc))
        metric = getattr(exc, "metric", "")
        raise OpenApiGuardError(str(code), metric=str(metric)) from exc
    return OpenApiGuardResult(request_id=normalized_request_id, reservation=reservation)

