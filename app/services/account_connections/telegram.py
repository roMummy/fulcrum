"""Telegram Bot API 账号身份连接器。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.channel_adapters.telegram import TelegramAdapter

from .contracts import AccountConnectionResult, VerifiedAccountIdentity


class TelegramAccountConnector:
    """通过 Telegram 官方 getMe 结果确认账号身份。"""

    channel_type = "telegram"

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        del db
        if channel.channel_type != self.channel_type:
            return AccountConnectionResult.failed("channel_type_mismatch", "渠道类型不匹配")
        health = TelegramAdapter().health_check(channel)
        details = health.get("details") if isinstance(health.get("details"), dict) else {}
        account_id = str(details.get("bot_id") or "").strip()
        username = str(details.get("username") or "").strip()
        if account_id:
            return AccountConnectionResult.verified(
                VerifiedAccountIdentity(
                    adapter_type=self.channel_type,
                    account_id=account_id,
                    display_name=f"@{username}" if username else f"Telegram {account_id}",
                    # Telegram 没有本地执行端设备，官方 Bot ID 是可验证且稳定的账号设备键。
                    device_key=f"telegram-bot:{account_id}",
                ),
                message="Telegram 账号验证成功",
            )
        return AccountConnectionResult.failed(
            str(details.get("reason") or "telegram_identity_missing"),
            str(health.get("summary") or "Telegram 凭证无效"),
        )
