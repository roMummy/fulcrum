from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Channel, OpenApiApp, OpenApiAppChannel
from app.services.channel_labels import require_saas_enabled_channel_type
from app.services.open_api_auth import set_open_api_secret


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


def list_open_api_apps(db: Session) -> list[OpenApiApp]:
    """按创建时间倒序列出开放接口应用。"""

    return db.scalars(select(OpenApiApp).order_by(OpenApiApp.id.desc())).all()


def list_authorized_channel_ids(db: Session, app_id: int) -> list[int]:
    """列出开放应用当前仍可用的企业微信授权渠道 ID。"""

    app = db.get(OpenApiApp, int(app_id or 0))
    if app is None:
        return []
    rows = db.scalars(
        select(OpenApiAppChannel.channel_id)
        .join(Channel, Channel.id == OpenApiAppChannel.channel_id)
        .where(
            OpenApiAppChannel.app_id == app.id,
            Channel.channel_type == "workwechat",
        )
    ).all()
    return [int(channel_id) for channel_id in rows]


def upsert_open_api_app(db: Session, app_id: int, data: OpenApiAppInput) -> OpenApiApp:
    """创建或更新开放接口应用。

    app_key 是业务系统调用 MBA 的稳定身份；更新时如果 app_secret 为空，
    会保留旧密钥，避免运维只改 webhook_url 时导致客户系统密钥失效。
    """

    app = db.get(OpenApiApp, int(app_id or 0)) if int(app_id or 0) > 0 else None
    normalized_key = data.app_key.strip()
    if not normalized_key:
        raise ValueError("app_key 不能为空")
    if not data.name.strip():
        raise ValueError("应用名称不能为空")
    channel = db.get(Channel, int(data.default_channel_id or 0))
    if channel is None:
        raise ValueError("默认渠道不存在")
    require_saas_enabled_channel_type(channel.channel_type)
    existing = db.scalar(select(OpenApiApp).where(OpenApiApp.app_key == normalized_key))
    if existing is not None and (app is None or int(existing.id) != int(app.id)):
        raise ValueError("app_key 已存在")
    if app is None:
        app = OpenApiApp(app_key=normalized_key)
        db.add(app)
    app.name = data.name.strip()
    app.app_key = normalized_key
    # 开放应用的真实权限边界是 open_api_app_channels 授权渠道表；
    # 不再默认绑定主渠道所属 Bot，避免一个企业多企微号、多机器人时被 Bot 维度误拦截。
    app.bot_id = int(data.bot_id or 0) or None
    app.default_channel_id = channel.id
    app.status = "disabled" if data.status.strip() == "disabled" else "active"
    app.webhook_url = data.webhook_url.strip() or None
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
    授权渠道可以来自不同 Bot，适配一个企业使用多个企微号共同服务同一业务系统。
    """

    normalized_ids = {int(item) for item in channel_ids if int(item or 0) > 0}
    normalized_ids.add(int(primary_channel_id))
    valid_channels = db.scalars(select(Channel).where(Channel.id.in_(normalized_ids))).all()
    valid_ids = {int(channel.id) for channel in valid_channels}
    if valid_ids != normalized_ids:
        raise ValueError("授权渠道不存在")
    for channel in valid_channels:
        # 不能通过多选授权字段把历史旧渠道重新暴露给外部系统。
        require_saas_enabled_channel_type(channel.channel_type)
    existing = db.scalars(select(OpenApiAppChannel).where(OpenApiAppChannel.app_id == app.id)).all()
    existing_by_channel = {int(row.channel_id): row for row in existing}
    for row in existing:
        if int(row.channel_id) not in valid_ids:
            db.delete(row)
    for channel_id in sorted(valid_ids):
        if channel_id not in existing_by_channel:
            db.add(OpenApiAppChannel(app_id=app.id, channel_id=channel_id))
