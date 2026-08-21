from __future__ import annotations

import pytest

from app.services.account_connections import (
    AccountConnectionError,
    VerifiedAccountIdentity,
    get_account_connector,
)


def test_unknown_account_connector_is_rejected() -> None:
    """未知渠道不能降级到通用 HTTP 连接器。"""

    with pytest.raises(AccountConnectionError, match="connection_type_not_supported"):
        get_account_connector("unknown")


def test_verified_identity_requires_stable_account_and_device() -> None:
    """商业绑定只能消费已经规范化的账号和设备身份。"""

    with pytest.raises(AccountConnectionError, match="account_identity_invalid"):
        VerifiedAccountIdentity(
            adapter_type="telegram",
            account_id="",
            display_name="Bot",
            device_key="",
        )


def test_verified_identity_normalizes_adapter_and_display_name() -> None:
    identity = VerifiedAccountIdentity(
        adapter_type="Telegram",
        account_id=" 10001 ",
        display_name="",
        device_key="telegram-bot:10001",
    )

    assert identity.adapter_type == "telegram"
    assert identity.account_id == "10001"
    assert identity.display_name == "10001"
