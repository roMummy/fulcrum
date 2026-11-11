from __future__ import annotations

from dataclasses import dataclass
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, OpenApiApp, OpenApiAppChannel
from app.services.billing import get_current_subscription, subscription_is_active
from app.services.channel_catalog import list_supported_channel_types, require_supported_channel_type
from app.services.open_api_auth import set_open_api_secret
from app.services.open_api_quotas import OpenApiQuotaError, require_open_api_resource_quota


@dataclass(frozen=True)
class OpenApiAppInput:
    """开放接口应用配置输入。"""

    name: str
    app_key: str
    app_secret: str
    default_channel_id: int
    bot_id: int = 0
    status: str = "active"
    webhook_url: str = ""
    webhook_secret: str = ""
    authorized_channel_ids: tuple[int, ...] = ()


class OpenApiAppError(ValueError):
    """开放应用自助管理失败时返回的稳定业务错误。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def list_open_api_apps(db: Session) -> list[OpenApiApp]:
    """按创建时间倒序列出开放接口应用。"""

    return db.scalars(
        select(OpenApiApp)
        .where(OpenApiApp.tenant_id == _require_tenant_id())
        .order_by(OpenApiApp.id.desc())
    ).all()


def require_active_open_api_subscription(db: Session) -> None:
    """只有有效订阅租户可以创建或修改开放应用。"""

    subscription = get_current_subscription(db, tenant_id=require_tenant_id())
    if not subscription_is_active(subscription):
        raise OpenApiAppError("subscription_required")


def generate_open_api_secret() -> str:
    """生成只展示一次的高熵开放应用密钥。"""

    return secrets.token_urlsafe(32)


def disable_open_api_app(db: Session, *, app_id: int) -> OpenApiApp:
    """停用当前租户开放应用，并保持历史任务可按原应用键审计。"""

    app = db.scalar(
        select(OpenApiApp).where(
            OpenApiApp.id == int(app_id or 0),
            OpenApiApp.tenant_id == _require_tenant_id(),
        )
    )
    if app is None:
        raise OpenApiAppError("app_not_found")
    app.status = "disabled"
    db.commit()
    db.refresh(app)
    return app


def rotate_open_api_secret(db: Session, *, app_id: int) -> tuple[OpenApiApp, str]:
    """轮换开放应用密钥，只把新明文返回给当前请求。"""

    app = db.scalar(
        select(OpenApiApp).where(
            OpenApiApp.id == int(app_id or 0),
            OpenApiApp.tenant_id == _require_tenant_id(),
        )
    )
    if app is None:
        raise OpenApiAppError("app_not_found")
    secret = generate_open_api_secret()
    set_open_api_secret(app, secret)
    db.commit()
    db.refresh(app)
    return app, secret


def _require_tenant_id() -> int:
    """延迟读取可信租户上下文，避免服务模块初始化时绑定请求状态。"""

    from app.core.tenant_context import require_tenant_id as read_tenant_id

    return int(read_tenant_id())


def list_authorized_channel_ids(db: Session, app_id: int) -> list[int]:
    """列出开放应用当前仍可用的正式授权渠道 ID。"""

    app = db.scalar(
        select(OpenApiApp).where(
            OpenApiApp.id == int(app_id or 0),
            OpenApiApp.tenant_id == _require_tenant_id(),
        )
    )
    if app is None:
        return []
    rows = db.scalars(
        select(OpenApiAppChannel.channel_id)
        .join(Channel, Channel.id == OpenApiAppChannel.channel_id)
        .where(
            OpenApiAppChannel.app_id == app.id,
            Channel.channel_type.in_(list_supported_channel_types()),
        )
    ).all()
    return [int(channel_id) for channel_id in rows]


def upsert_open_api_app(db: Session, app_id: int, data: OpenApiAppInput) -> OpenApiApp:
    """创建或更新开放接口应用。

    app_key 是业务系统调用 Fulcrum 的稳定身份；更新时如果 app_secret 为空，
    会保留旧密钥，避免运维只改 webhook_url 时导致客户系统密钥失效。
    """

    app = (
        db.scalar(
            select(OpenApiApp).where(
                OpenApiApp.id == int(app_id or 0),
                OpenApiApp.tenant_id == _require_tenant_id(),
            )
        )
        if int(app_id or 0) > 0
        else None
    )
    is_new = app is None
    normalized_key = data.app_key.strip()
    if not normalized_key:
        raise ValueError("app_key 不能为空")
    if not data.name.strip():
        raise ValueError("应用名称不能为空")
    channel = db.scalar(
        select(Channel).where(
            Channel.id == int(data.default_channel_id or 0),
            Channel.tenant_id == _require_tenant_id(),
            Channel.deleted_at.is_(None),
        )
    )
    if channel is None:
        raise ValueError("默认渠道不存在")
    require_supported_channel_type(channel.channel_type)
    existing = db.scalar(
        select(OpenApiApp).where(
            OpenApiApp.tenant_id == _require_tenant_id(),
            OpenApiApp.app_key == normalized_key,
        )
    )
    if existing is not None and (app is None or int(existing.id) != int(app.id)):
        raise ValueError("app_key 已存在")
    requested_channel_ids = {int(item) for item in data.authorized_channel_ids if int(item or 0) > 0}
    requested_channel_ids.add(int(channel.id))
    try:
        require_open_api_resource_quota(
            db,
            tenant_id=_require_tenant_id(),
            app_id=int(app.id) if app is not None and app.id is not None else None,
            requested_channel_ids=requested_channel_ids,
        )
    except OpenApiQuotaError as exc:
        raise OpenApiAppError(exc.code) from exc
    if is_new:
        app = OpenApiApp(app_key=normalized_key)
        db.add(app)
    app.name = data.name.strip()
    app.app_key = normalized_key
    # 开放应用的真实权限边界是 open_api_app_channels 授权渠道表；
    # 不再默认绑定主渠道所属 Bot，避免一个企业多渠道、多机器人时被 Bot 维度误拦截。
    app.bot_id = int(data.bot_id or 0) or None
    app.default_channel_id = channel.id
    app.status = "disabled" if data.status.strip() == "disabled" else "active"
    app.webhook_url = data.webhook_url.strip() or None
    if data.webhook_secret.strip() or is_new:
        app.webhook_secret = data.webhook_secret.strip() or None
    if data.app_secret.strip():
        set_open_api_secret(app, data.app_secret.strip())
    elif not app.app_secret_encrypted:
        raise ValueError("新建应用必须填写 app_secret")
    db.flush()
    _sync_authorized_channels(db, app=app, primary_channel_id=channel.id, channel_ids=data.authorized_channel_ids)
    db.commit()
    db.refresh(app)
    return app


def _sync_authorized_channels(db: Session, *, app: OpenApiApp, primary_channel_id: int, channel_ids: tuple[int, ...]) -> None:
    """同步开放应用授权渠道。

    主渠道用于后台识别应用归属，保存时自动纳入授权范围；发送接口仍必须显式传 channel_id。
    授权渠道可以来自不同 Bot，适配一个企业使用多个渠道共同服务同一业务系统。
    """

    normalized_ids = {int(item) for item in channel_ids if int(item or 0) > 0}
    normalized_ids.add(int(primary_channel_id))
    valid_channels = db.scalars(select(Channel).where(Channel.id.in_(normalized_ids), Channel.deleted_at.is_(None))).all()
    valid_ids = {int(channel.id) for channel in valid_channels}
    if valid_ids != normalized_ids:
        raise ValueError("授权渠道不存在")
    for channel in valid_channels:
        # 开放接口只能授权已经进入正式目录的渠道，未知类型不得借多选字段进入运行链路。
        require_supported_channel_type(channel.channel_type)
    existing = db.scalars(select(OpenApiAppChannel).where(OpenApiAppChannel.app_id == app.id)).all()
    existing_by_channel = {int(row.channel_id): row for row in existing}
    for row in existing:
        if int(row.channel_id) not in valid_ids:
            db.delete(row)
    for channel_id in sorted(valid_ids):
        if channel_id not in existing_by_channel:
            db.add(OpenApiAppChannel(app_id=app.id, channel_id=channel_id))
