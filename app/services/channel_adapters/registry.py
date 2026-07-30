from __future__ import annotations

from app.models.entities import Channel

from .base import BaseChannelAdapter
from .generic import GenericHttpAdapter
from .simulator import SimulatorAdapter
from .telegram import TelegramAdapter
from .wechat_ipad import WechatIpadAdapter
from .wechat_winauto import WechatWinAutoAdapter
from .workwechat import WorkWechatAdapter


def get_channel_adapter(channel: Channel) -> BaseChannelAdapter:
    if channel.channel_type == "wechat_ipad":
        return WechatIpadAdapter()
    if channel.channel_type == "wechat_winauto":
        return WechatWinAutoAdapter()
    if channel.channel_type == "workwechat":
        return WorkWechatAdapter()
    if channel.channel_type == "telegram":
        return TelegramAdapter()
    if channel.channel_type == "simulator":
        return SimulatorAdapter()
    return GenericHttpAdapter()
