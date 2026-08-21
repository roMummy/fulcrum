"""企业微信客户端账号身份连接器。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.tenant_context import require_tenant_id
from app.models.entities import Channel
from app.models.saas import ProviderNode, ProviderNodeAccount

from .contracts import AccountConnectionResult, VerifiedAccountIdentity


class WorkWechatAccountConnector:
    """只接受当前租户已激活节点发现的在线企业微信账号。"""

    channel_type = "workwechat"

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        if channel.channel_type != self.channel_type:
            return AccountConnectionResult.failed("channel_type_mismatch", "渠道类型不匹配")
        tenant_id = require_tenant_id()
        node = db.get(ProviderNode, int(channel.provider_node_id or 0))
        account = db.get(ProviderNodeAccount, int(channel.provider_account_id or 0))
        if node is None or node.tenant_id != tenant_id or node.status != "active":
            return AccountConnectionResult.connecting("请先连接企业微信客户端", next_action="connect_client")
        if not node.device_id:
            return AccountConnectionResult.connecting("客户端尚未完成激活", next_action="connect_client")
        if (
            account is None
            or account.tenant_id != tenant_id
            or account.provider_node_id != node.id
            or account.status != "online"
        ):
            return AccountConnectionResult.connecting("请选择当前在线的企业微信账号", next_action="select_account")
        account_id = str(account.external_user_id or account.account_key or "").strip()
        if not account_id:
            return AccountConnectionResult.failed("account_identity_missing", "客户端未上报账号标识")
        return AccountConnectionResult.verified(
            VerifiedAccountIdentity(
                adapter_type=self.channel_type,
                account_id=account_id,
                display_name=account.display_name or account_id,
                device_key=f"workwechat-device:{node.device_id}",
            ),
            message="企业微信账号验证成功",
        )
