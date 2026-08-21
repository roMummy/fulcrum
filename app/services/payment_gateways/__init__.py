"""付款方式抽象；首版只注册人工付款实现。"""

from app.services.payment_gateways.base import PaymentGateway, PaymentInstructions
from app.services.payment_gateways.manual import ManualPaymentGateway

__all__ = ["ManualPaymentGateway", "PaymentGateway", "PaymentInstructions"]
