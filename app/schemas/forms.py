from pydantic import BaseModel, Field


class BotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    command_prefix: str = "/"
    group_id: int | None = None
    config_template_id: int | None = None
    environment: str = "prod"
    tags: str | None = None
    is_enabled: bool = True


class BotUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    command_prefix: str = "/"
    group_id: int | None = None
    config_template_id: int | None = None
    environment: str = "prod"
    tags: str | None = None
    is_enabled: bool = True


class ChannelCreate(BaseModel):
    bot_id: int
    name: str = Field(..., min_length=1, max_length=100)
    channel_type: str = Field(..., min_length=1, max_length=50)
    api_base: str | None = None
    bot_token: str | None = None
    webhook_path: str | None = None
    dm_policy: str = "allowlist"
    allow_from: str | None = None
    adapter_config_json: str | None = None
    is_enabled: bool = True


class ChannelUpdate(BaseModel):
    bot_id: int
    name: str = Field(..., min_length=1, max_length=100)
    channel_type: str = Field(..., min_length=1, max_length=50)
    api_base: str | None = None
    bot_token: str | None = None
    webhook_path: str | None = None
    dm_policy: str = "allowlist"
    allow_from: str | None = None
    adapter_config_json: str | None = None
    is_enabled: bool = True


class BotPluginBindCreate(BaseModel):
    bot_id: int
    plugin_id: int
    status: str = "enabled"
    config_json: str | None = None
