"""面向租户的通用账号连接能力。"""

from .contracts import (
    AccountConnectionError,
    AccountConnectionResult,
    AccountConnector,
    VerifiedAccountIdentity,
)
from .registry import get_account_connector
from .service import (
    FinalizedAccountConnection,
    begin_account_connection,
    finalize_account_connection,
    verify_account_connection,
)

__all__ = [
    "AccountConnectionError",
    "AccountConnectionResult",
    "AccountConnector",
    "VerifiedAccountIdentity",
    "FinalizedAccountConnection",
    "begin_account_connection",
    "finalize_account_connection",
    "get_account_connector",
    "verify_account_connection",
]
