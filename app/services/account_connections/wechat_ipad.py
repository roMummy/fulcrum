"""微信 iPad 协议账号身份连接器。"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.entities import Channel

from .contracts import AccountConnectionResult, VerifiedAccountIdentity


def _load_adapter_config(channel: Channel) -> dict[str, object]:
    """只读取服务端保存的结构化登录结果，损坏 JSON 不参与身份判断。"""

    try:
        payload = json.loads(channel.adapter_config_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class WechatIpadAccountConnector:
    """在扫码登录成功后读取协议返回的 wxid 和设备 ID。"""

    channel_type = "wechat_ipad"

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        del db
        if channel.channel_type != self.channel_type:
            return AccountConnectionResult.failed("channel_type_mismatch", "渠道类型不匹配")
        if str(channel.login_state or "").strip() != "online":
            return AccountConnectionResult.connecting("请使用微信完成扫码登录", next_action="scan_qrcode")
        config = _load_adapter_config(channel)
        wxid = str(config.get("wxid") or "").strip()
        device_id = str(config.get("device_id") or "").strip()
        nickname = str(config.get("nickname") or config.get("nick_name") or "").strip()
        if not wxid or not device_id:
            return AccountConnectionResult.failed("account_identity_missing", "登录结果缺少账号或设备标识")
        return AccountConnectionResult.verified(
            VerifiedAccountIdentity(
                adapter_type=self.channel_type,
                account_id=wxid,
                display_name=nickname or wxid,
                device_key=f"wechat-ipad-device:{device_id}",
            ),
            message="个人微信账号验证成功",
        )
