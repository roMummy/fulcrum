"""微信 iOS（GugaTool）账号身份连接器。

Agent 模式下手机主动注册,平台只读注册快照,不再反向访问手机。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.guga_agent.service import registration

from .contracts import AccountConnectionResult, VerifiedAccountIdentity


class WechatGugatoolAccountConnector:
    """以手机注册快照里的 device_id/account_id 作为可信身份。"""

    channel_type = "wechat_gugatool"

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        del db
        if channel.channel_type != self.channel_type:
            return AccountConnectionResult.failed("channel_type_mismatch", "渠道类型不匹配")

        registered = registration(channel)
        account_id = str(registered.get("account_id") or "").strip()
        device_id = str(registered.get("device_id") or "").strip()
        if not account_id or not device_id:
            return AccountConnectionResult.failed(
                "account_identity_missing",
                "手机端尚未注册，请先在咕嘎助手中填写平台生成的服务器地址",
            )
        profile = {}
        try:
            import json

            runtime = json.loads(channel.runtime_info_json or "{}")
            profile = runtime.get("account_profile") if isinstance(runtime.get("account_profile"), dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            profile = {}
        nickname = str(profile.get("name") or "").strip() or account_id
        return AccountConnectionResult.verified(
            VerifiedAccountIdentity(
                adapter_type=self.channel_type,
                account_id=account_id,
                display_name=nickname,
                # 手机端没有独立设备标识,注册时上报的 device_id + wxid 是最稳定的归属键。
                device_key=f"guga-agent:{device_id}:{account_id}",
            ),
            message="微信账号验证成功",
        )
