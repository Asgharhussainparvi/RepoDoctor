"""A tiny in-memory payment gateway used by the fixture app."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GatewayReceipt:
    receipt_id: str
    customer_id: str
    amount_cents: int
    kind: str  # "charge" or "refund"


@dataclass
class FakeGateway:
    receipts: list[GatewayReceipt] = field(default_factory=list)

    def _record(self, customer_id: str, amount_cents: int, kind: str) -> GatewayReceipt:
        receipt = GatewayReceipt(
            receipt_id=f"rcpt_{len(self.receipts) + 1:04d}",
            customer_id=customer_id,
            amount_cents=amount_cents,
            kind=kind,
        )
        self.receipts.append(receipt)
        return receipt

    def charge(self, customer_id: str, amount_cents: int) -> GatewayReceipt:
        return self._record(customer_id, amount_cents, "charge")

    def refund(self, customer_id: str, amount_cents: int) -> GatewayReceipt:
        return self._record(customer_id, amount_cents, "refund")
