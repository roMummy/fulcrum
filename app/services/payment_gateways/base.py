"""与具体支付渠道无关的付款说明契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PaymentInstructions:
    """租户订单页可展示的规范化付款说明。"""

    title: str
    instructions: str
    qr_image: str
    bank_details: str


class PaymentGateway(Protocol):
    """付款适配器最小契约，订单核心不依赖微信、支付宝或银行 SDK。"""

    def get_instructions(self) -> PaymentInstructions:
        """返回当前付款方式的展示说明。"""

