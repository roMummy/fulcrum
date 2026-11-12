from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable


class ChannelTypeNotSupported(ValueError):
    """表示渠道类型未进入正式产品目录，调用方不得静默降级执行。"""

    def __init__(self, channel_type: str | None = None):
        self.channel_type = str(channel_type or "").strip()
        super().__init__("channel_type_not_supported")


class ChannelConfigurationError(ValueError):
    """表示启用渠道时缺少统一目录声明的必要配置。"""

    def __init__(self, channel_type: str, field_name: str, message: str):
        self.channel_type = channel_type
        self.field_name = field_name
        super().__init__(message)


@dataclass(frozen=True)
class ChannelConfigField:
    """描述渠道表单字段及启用校验规则，供页面和服务端共享。"""

    name: str
    label: str
    input_type: str = "text"
    placeholder: str = ""
    required_when_enabled: bool = False
    secret: bool = False
    rows: int = 0


@dataclass(frozen=True)
class ChannelTypeDefinition:
    """渠道稳定编码、运行能力和适配器工厂的唯一来源。"""

    code: str
    label: str
    summary: str
    runtime_mode: str
    capabilities: frozenset[str]
    config_fields: tuple[ChannelConfigField, ...]
    adapter_factory: Callable[[], Any]
    requires_executor_binding: bool = False
    # 平台托管端点表示租户不能填写或修改 api_base，连接服务必须从平台配置读取。
    platform_managed_endpoint: bool = False


def _build_workwechat_adapter():
    # 使用延迟导入，避免渠道目录与 channel_adapters 包初始化形成循环依赖。
    from app.services.channel_adapters.workwechat import WorkWechatAdapter

    return WorkWechatAdapter()


def _build_wechat_ipad_adapter():
    from app.services.channel_adapters.wechat_ipad import WechatIpadAdapter

    return WechatIpadAdapter()


def _build_wechat_winauto_adapter():
    from app.services.channel_adapters.wechat_winauto import WechatWinAutoAdapter

    return WechatWinAutoAdapter()


def _build_wechat_webridge_adapter():
    from app.services.channel_adapters.wechat_webridge import WechatWeBridgeAdapter

    return WechatWeBridgeAdapter()


def _build_wechat_gugatool_adapter():
    from app.services.channel_adapters.wechat_gugatool import WechatGugatoolAdapter

    return WechatGugatoolAdapter()


def _build_telegram_adapter():
    from app.services.channel_adapters.telegram import TelegramAdapter

    return TelegramAdapter()


COMMON_POLICY_FIELDS = (
    ChannelConfigField("webhook_path", "Webhook Path", placeholder="/webhooks/channel"),
    ChannelConfigField("dm_policy", "私聊策略", input_type="select"),
    ChannelConfigField("allow_from", "允许名单", input_type="textarea", rows=2),
    ChannelConfigField("adapter_config_json", "适配配置 JSON", input_type="textarea", rows=5),
)


CHANNEL_TYPE_DEFINITIONS = (
    ChannelTypeDefinition(
        code="workwechat",
        label="企业微信",
        summary="通过企业本地执行端接入企业微信账号、联系人和群聊。",
        runtime_mode="remote_executor",
        capabilities=frozenset({"send", "inbound", "health", "chatrooms", "contacts", "remote_executor"}),
        config_fields=(
            ChannelConfigField("provider_node_id", "执行端节点", input_type="executor_node", required_when_enabled=True),
            ChannelConfigField(
                "provider_account_id",
                "执行端账号",
                input_type="executor_account",
                required_when_enabled=True,
            ),
            ChannelConfigField("api_base", "API Base", placeholder="直连 HTTP 模式使用，可留空"),
            ChannelConfigField("bot_token", "Bot Token", input_type="password", secret=True),
            *COMMON_POLICY_FIELDS,
        ),
        adapter_factory=_build_workwechat_adapter,
        requires_executor_binding=True,
    ),
    ChannelTypeDefinition(
        code="wechat_ipad",
        label="微信 iPad 协议",
        summary="通过微信协议服务接入个人微信，支持登录、消息、联系人和群聊。",
        runtime_mode="platform_polling",
        capabilities=frozenset({"send", "inbound", "health", "chatrooms", "contacts", "login", "polling"}),
        config_fields=(
            *COMMON_POLICY_FIELDS,
        ),
        adapter_factory=_build_wechat_ipad_adapter,
        platform_managed_endpoint=True,
    ),
    ChannelTypeDefinition(
        code="wechat_winauto",
        label="微信 Windows",
        summary="通过 Windows 微信执行端接入个人微信消息和群聊。",
        runtime_mode="webhook",
        capabilities=frozenset({"send", "inbound", "health", "chatrooms"}),
        config_fields=(
            ChannelConfigField("api_base", "API Base", placeholder="http://127.0.0.1:8182", required_when_enabled=True),
            ChannelConfigField("bot_token", "访问凭证", input_type="password", secret=True),
            *COMMON_POLICY_FIELDS,
        ),
        adapter_factory=_build_wechat_winauto_adapter,
    ),
    ChannelTypeDefinition(
        code="wechat_webridge",
        label="微桥 WeBridge",
        summary="通过 iPhone 微信微桥插件的服务端长轮询接入个人微信消息。",
        runtime_mode="platform_polling",
        capabilities=frozenset({
            "send", "inbound", "health", "polling", "advanced_commands", "media",
            "contacts", "chatrooms", "payments", "profile", "moments", "favorites", "official_accounts",
        }),
        config_fields=(
            ChannelConfigField("api_base", "WeBridge 服务端地址", placeholder="https://example.com/api/webridge/CHANNEL_ID", required_when_enabled=True),
            *COMMON_POLICY_FIELDS,
        ),
        adapter_factory=_build_wechat_webridge_adapter,
    ),
    ChannelTypeDefinition(
        code="wechat_gugatool",
        label="咕嘎助手",
        summary="通过 iPhone 上的咕嘎助手客户端主动连接平台，收发消息并管理群。",
        runtime_mode="platform_polling",
        capabilities=frozenset({"send", "inbound", "health", "polling", "media", "chatrooms"}),
        config_fields=(
            *COMMON_POLICY_FIELDS,
        ),
        adapter_factory=_build_wechat_gugatool_adapter,
    ),
    ChannelTypeDefinition(
        code="telegram",
        label="Telegram",
        summary="通过 Telegram Bot API 接入私聊和群聊消息。",
        runtime_mode="platform_polling",
        capabilities=frozenset({"send", "inbound", "health", "polling"}),
        config_fields=(
            ChannelConfigField("bot_token", "Bot Token", input_type="password", required_when_enabled=True, secret=True),
            ChannelConfigField("api_base", "API Base", placeholder="https://api.telegram.org"),
            *COMMON_POLICY_FIELDS,
        ),
        adapter_factory=_build_telegram_adapter,
    ),
)

_CHANNEL_TYPE_BY_CODE = {definition.code: definition for definition in CHANNEL_TYPE_DEFINITIONS}


def list_channel_type_definitions() -> tuple[ChannelTypeDefinition, ...]:
    """按产品展示顺序返回正式渠道定义。"""

    return CHANNEL_TYPE_DEFINITIONS


def list_supported_channel_types() -> tuple[str, ...]:
    """返回所有允许租户创建、编辑和运行的稳定渠道编码。"""

    return tuple(definition.code for definition in CHANNEL_TYPE_DEFINITIONS)


def serialize_channel_type_definitions() -> tuple[dict[str, Any], ...]:
    """输出不包含适配器工厂的页面元数据，避免模板复制渠道字段和必填规则。"""

    return tuple(
        {
            "code": definition.code,
            "label": definition.label,
            "summary": definition.summary,
            "runtime_mode": definition.runtime_mode,
            "capabilities": sorted(definition.capabilities),
            "requires_executor_binding": definition.requires_executor_binding,
            "platform_managed_endpoint": definition.platform_managed_endpoint,
            "config_fields": [
                {
                    "name": field.name,
                    "label": field.label,
                    "input_type": field.input_type,
                    "placeholder": field.placeholder,
                    "required_when_enabled": field.required_when_enabled,
                    "secret": field.secret,
                    "rows": field.rows,
                }
                for field in definition.config_fields
            ],
        }
        for definition in CHANNEL_TYPE_DEFINITIONS
    )


def get_channel_type_definition(value: str | None) -> ChannelTypeDefinition:
    """读取正式渠道定义；未知类型必须显式失败，禁止隐式降级。"""

    normalized = str(value or "").strip()
    definition = _CHANNEL_TYPE_BY_CODE.get(normalized)
    if definition is None:
        raise ChannelTypeNotSupported(normalized)
    return definition


def require_supported_channel_type(value: str | None) -> str:
    """规范化并校验渠道编码，供页面、Schema 和开放接口共同复用。"""

    return get_channel_type_definition(value).code


def channel_supports(channel_type: str | None, capability: str) -> bool:
    """判断正式渠道是否声明某项能力，调用方无需识别具体协议实现。"""

    definition = get_channel_type_definition(channel_type)
    return str(capability or "").strip() in definition.capabilities


def validate_enabled_channel_configuration(
    channel_type: str | None,
    *,
    is_enabled: bool,
    field_values: Mapping[str, Any],
    configured_secret_fields: Iterable[str] = (),
) -> None:
    """按渠道目录校验启用所需配置，停用记录可以保存为不完整草稿。"""

    if not is_enabled:
        return

    definition = get_channel_type_definition(channel_type)
    configured_secrets = {str(name).strip() for name in configured_secret_fields}
    for field in definition.config_fields:
        if not field.required_when_enabled:
            continue
        value = field_values.get(field.name)
        # 敏感字段编辑时允许不回填明文；只要数据库已经保存密文，就视为仍然配置有效。
        if field.secret and field.name in configured_secrets:
            continue
        if isinstance(value, str):
            configured = bool(value.strip())
        else:
            configured = bool(value)
        if configured:
            continue
        raise ChannelConfigurationError(
            definition.code,
            field.name,
            f"启用{definition.label}前请填写{field.label}",
        )
