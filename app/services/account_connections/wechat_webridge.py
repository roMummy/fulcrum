from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.wechat_webridge.service import _runtime

from .contracts import AccountConnectionResult, VerifiedAccountIdentity


class WechatWeBridgeAccountConnector:
    """从 WeBridge 最近一次 register 快照确认账号，不主动猜测或伪造身份。"""

    channel_type = "wechat_webridge"

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        snapshot = _runtime(channel).get("webridge")
        if not isinstance(snapshot, dict) or not snapshot.get("device_id") or not snapshot.get("account_id"):
            return AccountConnectionResult.connecting("等待手机连接，请将长轮询地址复制到微桥插件", next_action="configure")
        profile = _runtime(channel).get("account_profile")
        profile = profile if isinstance(profile, dict) else {}
        identity = VerifiedAccountIdentity(
            adapter_type=self.channel_type,
            account_id=str(snapshot["account_id"]),
            display_name=str(profile.get("nickname") or snapshot.get("account_id") or "WeBridge 微信账号"),
            device_key=f"webridge-device:{snapshot['device_id']}",
        )
        return AccountConnectionResult.verified(identity, message="WeBridge 设备已连接")
