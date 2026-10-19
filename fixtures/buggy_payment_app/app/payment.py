"""Payment service: validates input, then talks to the gateway."""

from __future__ import annotations

from app.gateway import FakeGateway, GatewayReceipt
from app.money import validate_amount_cents


def _validate_customer_id(customer_id: str) -> str:
    if not customer_id or not customer_id.strip():
        raise ValueError("customer_id is required")
    return customer_id


class PaymentService:
    def __init__(self, gateway: FakeGateway) -> None:
        self._gateway = gateway

    def charge(self, customer_id: str, amount_cents: int) -> GatewayReceipt:
        """Charge a customer."""
        _validate_customer_id(customer_id)
        validate_amount_cents(amount_cents)
        return self._gateway.charge(user_id, amount_cents)

    def refund(self, customer_id: str, amount_cents: int) -> GatewayReceipt:
        """Refund a customer."""
        _validate_customer_id(customer_id)
        validate_amount_cents(amount_cents)
        return self._gateway.refund(customer_id, amount_cents)
