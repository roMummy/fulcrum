"""通用账号连接契约，核心商业逻辑不依赖具体 Provider 实现。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from app.models.entities import Channel
from app.services.bot_entitlements import normalize_adapter_type, normalize_bot_account_id


class AccountConnectionError(ValueError):
    """表示账号连接类型、身份或连接状态不满足正式产品要求。"""

    def __init__(self, code: str):
        self.code = str(code or "account_connection_failed").strip() or "account_connection_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class VerifiedAccountIdentity:
    """渠道服务端确认的稳定账号身份和设备归属来源。"""

    adapter_type: str
    account_id: str
    display_name: str
    device_key: str

    def __post_init__(self) -> None:
        # 在契约边界统一规范化，避免具体渠道把空白账号或大小写漂移带入商业绑定。
        try:
            normalized_adapter = normalize_adapter_type(self.adapter_type)
            normalized_account = normalize_bot_account_id(self.account_id)
        except ValueError as exc:
            raise AccountConnectionError("account_identity_invalid") from exc
        normalized_device = str(self.device_key or "").strip()
        if len(normalized_device) < 8 or len(normalized_device) > 512:
            raise AccountConnectionError("account_identity_invalid")
        normalized_display = str(self.display_name or "").strip() or normalized_account
        object.__setattr__(self, "adapter_type", normalized_adapter)
        object.__setattr__(self, "account_id", normalized_account)
        object.__setattr__(self, "display_name", normalized_display[:150])
        object.__setattr__(self, "device_key", normalized_device)


@dataclass(frozen=True)
class AccountConnectionResult:
    """渠道连接检查的统一结果，页面不需要理解具体协议返回结构。"""

    state: Literal["connecting", "verified", "failed"]
    identity: VerifiedAccountIdentity | None
    next_action: str
    message: str
    error_code: str = ""

    @classmethod
    def connecting(cls, message: str, *, next_action: str) -> "AccountConnectionResult":
        return cls(
            state="connecting",
            identity=None,
            next_action=str(next_action or "retry").strip() or "retry",
            message=str(message or "账号仍在连接中").strip(),
        )

    @classmethod
    def verified(cls, identity: VerifiedAccountIdentity, *, message: str = "账号验证成功") -> "AccountConnectionResult":
        return cls(
            state="verified",
            identity=identity,
            next_action="confirm",
            message=str(message or "账号验证成功").strip(),
        )

    @classmethod
    def failed(cls, code: str, message: str, *, next_action: str = "retry") -> "AccountConnectionResult":
        return cls(
            state="failed",
            identity=None,
            next_action=str(next_action or "retry").strip() or "retry",
            message=str(message or "账号连接失败").strip(),
            error_code=str(code or "account_connection_failed").strip() or "account_connection_failed",
        )


class AccountConnector(Protocol):
    """具体渠道只负责验证身份，不创建机器人、绑定或修改额度。"""

    channel_type: str

    def verify(self, db: Session, channel: Channel) -> AccountConnectionResult:
        ...
