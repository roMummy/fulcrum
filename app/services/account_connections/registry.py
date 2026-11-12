"""账号连接器严格注册表。"""

from __future__ import annotations

from .contracts import AccountConnectionError, AccountConnector


def get_account_connector(channel_type: str | None) -> AccountConnector:
    """返回正式渠道连接器；未知类型不得降级到通用 HTTP 实现。"""

    normalized = str(channel_type or "").strip()
    if normalized == "telegram":
        from .telegram import TelegramAccountConnector

        return TelegramAccountConnector()
    if normalized == "workwechat":
        from .workwechat import WorkWechatAccountConnector

        return WorkWechatAccountConnector()
    if normalized == "wechat_ipad":
        from .wechat_ipad import WechatIpadAccountConnector

        return WechatIpadAccountConnector()
    if normalized == "wechat_winauto":
        from .wechat_winauto import WechatWinAutoAccountConnector

        return WechatWinAutoAccountConnector()
    if normalized == "wechat_webridge":
        from .wechat_webridge import WechatWeBridgeAccountConnector

        return WechatWeBridgeAccountConnector()
    if normalized == "wechat_gugatool":
        from .wechat_gugatool import WechatGugatoolAccountConnector

        return WechatGugatoolAccountConnector()
    raise AccountConnectionError("connection_type_not_supported")
