"""Money helpers. All amounts are integer cents."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

MAX_AMOUNT_CENTS = 100_000_000  # $1,000,000.00


def to_cents(amount: str) -> int:
    """Convert a decimal string like '12.50' to integer cents."""
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {amount!r}") from exc
    if value < 0:
        raise ValueError("amount must not be negative")
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError("amount has more than two decimal places")
    return int(cents)


def format_cents(cents: int) -> str:
    """Format integer cents as a dollar string, e.g. 123456 -> '$1,234.56'."""
    dollars, remainder = divmod(cents, 100)
    return f"${dollars:,}.{remainder:02d}"


def validate_amount_cents(cents: int) -> int:
    """Return cents if it is a positive amount within the allowed maximum."""
    if cents <= 0:
        raise ValueError("amount must be positive")
    if cents > MAX_AMOUNT_CENTS:
        raise ValueError("amount exceeds maximum")
    return cents
