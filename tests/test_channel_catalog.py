from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.entities import Channel, Conversation
from app.schemas.forms import ChannelCreate, ChannelUpdate
from app.services.channel_adapters.base import BaseChannelAdapter
from app.services.channel_adapters.registry import get_channel_adapter
from app.services.channel_catalog import (
    ChannelTypeNotSupported,
    get_channel_type_definition,
    list_supported_channel_types,
)


def test_public_channel_catalog_contains_real_supported_channels() -> None:
    """租户正式入口只开放已经具备真实适配能力的四种渠道。"""

    assert list_supported_channel_types() == (
        "workwechat",
        "wechat_ipad",
        "wechat_winauto",
        "wechat_webridge",
        "wechat_gugatool",
        "telegram",
    )


def test_public_channel_catalog_uses_business_platform_names() -> None:
    """租户页面展示业务平台名称，不能暴露 Hook 等内部实现术语。"""

    assert get_channel_type_definition("workwechat").label == "企业微信"
    # 咕嘎助手是对外产品名，渠道编码同样使用 wechat_gugatool。
    assert get_channel_type_definition("wechat_gugatool").label == "咕嘎助手"


def test_wechat_ipad_catalog_has_no_channel_level_login_proxy_fields() -> None:
    """扫码代理属于单次登录会话，渠道目录不得再声明任何长期代理字段。"""

    fields = {field.name for field in get_channel_type_definition("wechat_ipad").config_fields}

    assert {"proxy_host", "proxy_port", "proxy_username", "proxy_password"}.isdisjoint(fields)


@pytest.mark.parametrize("channel_type", list_supported_channel_types())
def test_channel_schema_accepts_supported_channel_types(channel_type: str) -> None:
    """创建和编辑输入模型必须共享同一份渠道目录，不能再写死企微允许列表。"""

    created = ChannelCreate(bot_id=1, name="测试渠道", channel_type=channel_type, is_enabled=False)
    updated = ChannelUpdate(bot_id=1, name="测试渠道", channel_type=channel_type, is_enabled=False)

    assert created.channel_type == channel_type
    assert updated.channel_type == channel_type


@pytest.mark.parametrize("schema_type", (ChannelCreate, ChannelUpdate))
def test_channel_schema_rejects_unknown_channel_type(schema_type) -> None:
    """未注册类型不能通过表单模型进入数据库，也不能降级成通用 HTTP 渠道。"""

    with pytest.raises(ValidationError, match="channel_type_not_supported"):
        schema_type(bot_id=1, name="未知渠道", channel_type="unknown-adapter", is_enabled=False)


def test_unknown_channel_type_is_rejected_without_generic_fallback() -> None:
    """运行时遇到未知渠道必须显式失败，避免把错误配置发送到不可预期的地址。"""

    channel = Channel(bot_id=1, name="未知渠道", channel_type="unknown-adapter")

    with pytest.raises(ChannelTypeNotSupported, match="channel_type_not_supported"):
        get_channel_adapter(channel)


@pytest.mark.parametrize("channel_type", list_supported_channel_types())
def test_channel_catalog_builds_matching_adapter(channel_type: str) -> None:
    """目录注册的适配器工厂必须与稳定渠道编码一致。"""

    definition = get_channel_type_definition(channel_type)
    channel = Channel(bot_id=1, name="测试渠道", channel_type=channel_type)
    adapter = get_channel_adapter(channel)

    assert isinstance(adapter, BaseChannelAdapter)
    assert adapter.adapter_type == definition.code


def test_adapter_standard_result_uses_adapter_type() -> None:
    """通用业务结果使用适配类型，不再把具体执行端 Provider 当作产品字段。"""

    definition = get_channel_type_definition("telegram")
    adapter = definition.adapter_factory()
    conversation = Conversation(bot_id=1, channel_id=1, external_id="chat-1", chat_type="direct")

    result = adapter.build_result(
        ok=False,
        conversation=conversation,
        message_type="text",
        transport_status="error",
        status="failed",
        error_code="test_failure",
    )

    assert result["adapter_type"] == "telegram"
    assert "provider" not in result
