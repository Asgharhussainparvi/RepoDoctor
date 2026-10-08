import pytest

from app.money import MAX_AMOUNT_CENTS, format_cents, to_cents, validate_amount_cents


@pytest.mark.parametrize(
    ("text", "cents"),
    [("0.01", 1), ("1", 100), ("12.50", 1250), ("100.00", 10000), ("0.10", 10), ("999999.99", 99999999)],
)
def test_to_cents(text, cents):
    assert to_cents(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "-1.00", "1.234"])
def test_to_cents_rejects_bad_input(text):
    with pytest.raises(ValueError):
        to_cents(text)


@pytest.mark.parametrize(
    ("cents", "text"),
    [(1, "$0.01"), (100, "$1.00"), (1250, "$12.50"), (123456, "$1,234.56")],
)
def test_format_cents(cents, text):
    assert format_cents(cents) == text


@pytest.mark.parametrize("cents", [1, 100, MAX_AMOUNT_CENTS])
def test_validate_amount_accepts_valid(cents):
    assert validate_amount_cents(cents) == cents


@pytest.mark.parametrize("cents", [0, -5, MAX_AMOUNT_CENTS + 1])
def test_validate_amount_rejects_invalid(cents):
    with pytest.raises(ValueError):
        validate_amount_cents(cents)
