from pydantic import BaseModel, Field, field_validator

from app.services.channel_labels import require_saas_enabled_channel_type


class BotCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class BotUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None


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
    # 企业微信渠道必须绑定已验证的 Provider 节点账号；其他渠道保持为空。
    provider_node_id: int | None = None
    provider_account_id: int | None = None
    is_enabled: bool = True

    @field_validator("channel_type")
    @classmethod
    def validate_channel_type(cls, value: str) -> str:
        """阻止后台脚本或后续批量入口绕过页面创建非企微渠道。"""

        return require_saas_enabled_channel_type(value)


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
    # 编辑时仍由服务端重新校验租户和父子关系，不能只信任页面下拉选项。
    provider_node_id: int | None = None
    provider_account_id: int | None = None
    is_enabled: bool = True

    @field_validator("channel_type")
    @classmethod
    def validate_channel_type(cls, value: str) -> str:
        """编辑同样执行产品范围校验，不能把企微渠道改回历史类型。"""

        return require_saas_enabled_channel_type(value)


class BotPluginBindCreate(BaseModel):
    bot_id: int
    plugin_id: int
    status: str = "enabled"
    config_json: str | None = None
