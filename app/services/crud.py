from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Bot, BotPlugin, Channel, Plugin
from app.schemas.forms import BotCreate, BotPluginBindCreate, BotUpdate, ChannelCreate, ChannelUpdate
from app.services.runtime import compute_binding_runtime
from app.services.security import encrypt_secret, mask_secret


def create_bot(db: Session, data: BotCreate) -> Bot:
    # 创建入口只收集业务名称，其余运行状态由系统统一初始化，避免租户在首次
    # 接入企业微信前就面对环境、模板、前缀等没有实际价值的技术选项。
    bot = Bot(
        name=data.name.strip(),
        description=None,
        is_enabled=True,
        status="running",
    )
    db.add(bot)
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


def create_channel(db: Session, data: ChannelCreate) -> Channel:
    token = (data.bot_token or "").strip() or None
    adapter_config = (data.adapter_config_json or "").strip() or None
    channel = Channel(
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
        status="online" if data.is_enabled else "offline",
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return channel


def update_channel(db: Session, channel: Channel, data: ChannelUpdate) -> Channel:
    token = (data.bot_token or "").strip()
    channel.bot_id = data.bot_id
    channel.name = data.name.strip()
    channel.channel_type = data.channel_type.strip()
    channel.api_base = (data.api_base or "").strip() or None
    if token:
        channel.bot_token_encrypted = encrypt_secret(token)
        channel.bot_token_masked = mask_secret(token)
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
