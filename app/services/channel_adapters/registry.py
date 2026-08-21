from __future__ import annotations

from app.models.entities import Channel
from app.services.channel_catalog import get_channel_type_definition

from .base import BaseChannelAdapter


def get_channel_adapter(channel: Channel) -> BaseChannelAdapter:
    """从统一渠道目录构造适配器，未知类型不得降级到通用 HTTP 请求。"""

    definition = get_channel_type_definition(channel.channel_type)
    adapter = definition.adapter_factory()
    if not isinstance(adapter, BaseChannelAdapter):
        raise TypeError("channel_adapter_factory_invalid")
    return adapter
