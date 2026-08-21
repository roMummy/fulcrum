"""面向租户的通用账号连接能力。"""

from .contracts import (
    AccountConnectionError,
    AccountConnectionResult,
    AccountConnector,
    VerifiedAccountIdentity,
)
from .registry import get_account_connector

__all__ = [
    "AccountConnectionError",
    "AccountConnectionResult",
    "AccountConnector",
    "VerifiedAccountIdentity",
    "get_account_connector",
]
