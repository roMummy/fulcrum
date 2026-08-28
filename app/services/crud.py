from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.models.entities import Bot, BotPlugin, Channel, Plugin
from app.schemas.forms import BotCreate, BotPluginBindCreate, BotUpdate, ChannelCreate, ChannelUpdate
from app.services.channel_catalog import get_channel_type_definition
from app.services.runtime import compute_binding_runtime
from app.services.security import encrypt_secret, mask_secret


def _channel_declares_bot_token(channel_type: str) -> bool:
    """以统一渠道目录判断是否允许保存 Token，拒绝隐藏表单字段绕过产品配置。"""

    definition = get_channel_type_definition(channel_type)
    return any(field.name == "bot_token" for field in definition.config_fields)


def add_bot(
    db: Session,
    data: BotCreate,
    *,
    is_enabled: bool = True,
    status: str | None = None,
) -> Bot:
    """把机器人加入当前租户事务，不在内部提交。"""

    # 创建入口只收集业务名称，其余运行状态由系统统一初始化，避免租户在首次
    # 接入企业微信前就面对环境、模板、前缀等没有实际价值的技术选项。
    bot = Bot(
        tenant_id=require_tenant_id(),
        name=data.name.strip(),
        description=None,
        is_enabled=is_enabled,
        status=(status or ("running" if is_enabled else "stopped")).strip(),
    )
    db.add(bot)
    db.flush()
    return bot


def create_bot(db: Session, data: BotCreate) -> Bot:
    """创建并独立提交机器人，供非组合型后台动作使用。"""

    bot = add_bot(db, data)
    db.commit()
    db.refresh(bot)
    return bot


def update_bot(db: Session, bot: Bot, data: BotUpdate) -> Bot:
    bot.name = data.name.strip()
    bot.description = (data.description or "").strip() or None
    # 启停由独立动作负责，编辑名称或说明时不能意外改变机器人运行状态。
    db.commit()
    db.refresh(bot)
    return bot


def toggle_bot(db: Session, bot: Bot) -> Bot:
    bot.is_enabled = not bot.is_enabled
    bot.status = "running" if bot.is_enabled else "stopped"
    db.commit()
    db.refresh(bot)
    return bot


def get_bot(db: Session, bot_id: int) -> Bot | None:
    return db.get(Bot, bot_id)


def add_channel(
    db: Session,
    data: ChannelCreate,
    *,
    status: str | None = None,
) -> Channel:
    """把渠道加入当前租户事务，并统一处理敏感字段。"""

    # 密钥能力由渠道目录声明；874 未声明 Token，因此即使请求夹带该字段也不会落库。
    token = (data.bot_token or "").strip() or None if _channel_declares_bot_token(data.channel_type) else None
    adapter_config = (data.adapter_config_json or "").strip() or None
    channel = Channel(
        tenant_id=require_tenant_id(),
        bot_id=data.bot_id,
        name=data.name.strip(),
        channel_type=data.channel_type.strip(),
        api_base=(data.api_base or "").strip() or None,
        bot_token_encrypted=encrypt_secret(token),
        webhook_path=(data.webhook_path or "").strip() or None,
        dm_policy=data.dm_policy,
        allow_from=(data.allow_from or "").strip() or None,
        adapter_config_json=adapter_config,
        provider_node_id=data.provider_node_id,
        provider_account_id=data.provider_account_id,
        bot_token_masked=mask_secret(token),
        is_enabled=data.is_enabled,
        status=(status or ("online" if data.is_enabled else "offline")).strip(),
    )
    db.add(channel)
    db.flush()
    return channel


def update_channel(db: Session, channel: Channel, data: ChannelUpdate) -> Channel:
    accepts_bot_token = _channel_declares_bot_token(channel.channel_type)
    token = (data.bot_token or "").strip() if accepts_bot_token else ""
    if data.channel_type.strip() != channel.channel_type:
        # 渠道类型决定密钥结构、运行方式和外部账号身份，禁止通过通用更新服务换型。
        raise ValueError("channel_type_immutable")
    channel.bot_id = data.bot_id
    channel.name = data.name.strip()
    channel.api_base = (data.api_base or "").strip() or None
    if token:
        channel.bot_token_encrypted = encrypt_secret(token)
        channel.bot_token_masked = mask_secret(token)
    elif not accepts_bot_token:
        # 目录已经删除的密钥字段必须同步清空，不能在数据库中保留不可见的废弃配置。
        channel.bot_token_encrypted = None
        channel.bot_token_masked = None
    channel.webhook_path = (data.webhook_path or "").strip() or None
    channel.dm_policy = data.dm_policy
    channel.allow_from = (data.allow_from or "").strip() or None
    channel.adapter_config_json = (data.adapter_config_json or "").strip() or None
    channel.provider_node_id = data.provider_node_id
    channel.provider_account_id = data.provider_account_id
    channel.is_enabled = data.is_enabled
    channel.status = "online" if data.is_enabled else "offline"
    db.commit()
    db.refresh(channel)
    return channel


def toggle_channel(db: Session, channel: Channel) -> Channel:
    channel.is_enabled = not channel.is_enabled
    channel.status = "online" if channel.is_enabled else "offline"
    db.commit()
    db.refresh(channel)
    return channel


def get_channel(db: Session, channel_id: int) -> Channel | None:
    return db.get(Channel, channel_id)


def delete_bot(db: Session, bot: Bot) -> None:
    db.delete(bot)
    db.commit()


def bind_plugin(db: Session, data: BotPluginBindCreate) -> BotPlugin:
    existing = db.scalar(
        select(BotPlugin).where(BotPlugin.bot_id == data.bot_id, BotPlugin.plugin_id == data.plugin_id)
    )
    if existing:
        existing.status = data.status
        existing.config_json = data.config_json
        existing.runtime_status = compute_binding_runtime(existing)
        db.commit()
        db.refresh(existing)
        return existing

    binding = BotPlugin(
        bot_id=data.bot_id,
        plugin_id=data.plugin_id,
        status=data.status,
        runtime_status="running" if data.status == "enabled" else "disabled",
        config_json=data.config_json,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding


def get_binding(db: Session, binding_id: int) -> BotPlugin | None:
    return db.get(BotPlugin, binding_id)


def toggle_binding(db: Session, binding: BotPlugin) -> BotPlugin:
    binding.status = "disabled" if binding.status == "enabled" else "enabled"
    binding.runtime_status = compute_binding_runtime(binding)
    db.commit()
    db.refresh(binding)
    return binding


def update_binding_config(db: Session, binding: BotPlugin, status: str, config_json: str | None) -> BotPlugin:
    binding.status = status
    binding.runtime_status = compute_binding_runtime(binding)
    binding.config_json = config_json
    db.commit()
    db.refresh(binding)
    return binding


def list_bots(db: Session):
    return db.scalars(select(Bot).order_by(Bot.id.desc())).all()


def list_channels(db: Session):
    return db.scalars(select(Channel).order_by(Channel.id.desc())).all()


def list_plugins(db: Session):
    return db.scalars(select(Plugin).order_by(Plugin.id.desc())).all()
