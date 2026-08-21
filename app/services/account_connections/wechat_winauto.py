"""Windows 微信客户端账号身份连接器。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.channel_adapters.wechat_winauto import WechatWinAutoAdapter

from .contracts import AccountConnectionResult, VerifiedAccountIdentity


def _identity_payload(health: dict[str, Any]) -> dict[str, Any]:
    """从规定的健康响应层级读取身份，不使用 IP 或表单值伪造账号。"""

    details = health.get("details") if isinstance(health.get("details"), dict) else {}
    response = details.get("response") if isinstance(details.get("response"), dict) else {}
    data = response.get("data") if isinstance(response.get("data"), dict) else response
    account = data.get("account") if isinstance(data.get("account"), dict) else data
    return account if isinstance(account, dict) else {}


class WechatWinAutoAccountConnector:
    """要求 Windows 客户端健康接口返回稳定账号和设备标识。"""

    channel_type = "wechat_winauto"

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        del db
        if channel.channel_type != self.channel_type:
            return AccountConnectionResult.failed("channel_type_mismatch", "渠道类型不匹配")
        health = WechatWinAutoAdapter().health_check(channel)
        if not bool(health.get("ok")):
            return AccountConnectionResult.connecting(
                str(health.get("summary") or "请连接 Windows 微信客户端"),
                next_action="connect_client",
            )
        identity = _identity_payload(health)
        account_id = str(identity.get("account_id") or "").strip()
        device_id = str(identity.get("device_id") or "").strip()
        display_name = str(identity.get("display_name") or "").strip()
        if not account_id or not device_id:
            return AccountConnectionResult.failed(
                "account_identity_missing",
                "Windows 客户端未返回账号或设备标识",
                next_action="update_client",
            )
        return AccountConnectionResult.verified(
            VerifiedAccountIdentity(
                adapter_type=self.channel_type,
                account_id=account_id,
                display_name=display_name or account_id,
                device_key=f"wechat-windows-device:{device_id}",
            ),
            message="Windows 微信账号验证成功",
        )
