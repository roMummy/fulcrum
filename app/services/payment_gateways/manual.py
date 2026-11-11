"""人工转账付款适配器。"""

from __future__ import annotations

from app.core.settings import settings
from app.services.payment_gateways.base import PaymentInstructions


class ManualPaymentGateway:
    """从部署配置读取付款说明，不发起任何外部网络请求。"""

    def get_instructions(self) -> PaymentInstructions:
        """返回经过空白清理的人工付款展示数据。"""

        return PaymentInstructions(
            title=settings.billing_payment_title.strip() or "Fulcrum 人工付款",
            instructions=settings.billing_payment_instructions.strip(),
            qr_image=settings.billing_payment_qr_image.strip(),
            bank_details=settings.billing_payment_bank_details.strip(),
        )
