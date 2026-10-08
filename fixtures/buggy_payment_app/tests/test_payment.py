import pytest

from app.gateway import FakeGateway
from app.payment import PaymentService


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
def service(gateway):
    return PaymentService(gateway)


def test_charge_records_receipt_for_customer(service, gateway):
    receipt = service.charge("cust_42", 1250)
    assert receipt.customer_id == "cust_42"
    assert receipt.amount_cents == 1250
    assert receipt.kind == "charge"
    assert gateway.receipts == [receipt]


@pytest.mark.parametrize("amount", [0, -1])
def test_charge_rejects_non_positive_amount(service, amount):
    with pytest.raises(ValueError):
        service.charge("cust_42", amount)


@pytest.mark.parametrize("customer_id", ["", "   "])
def test_charge_rejects_blank_customer(service, customer_id):
    with pytest.raises(ValueError):
        service.charge(customer_id, 100)


def test_invalid_charge_never_reaches_gateway(service, gateway):
    with pytest.raises(ValueError):
        service.charge("cust_42", 0)
    assert gateway.receipts == []


def test_refund_records_receipt(service, gateway):
    receipt = service.refund("cust_42", 500)
    assert (receipt.customer_id, receipt.amount_cents, receipt.kind) == ("cust_42", 500, "refund")
    assert gateway.receipts == [receipt]


@pytest.mark.parametrize("amount", [0, -1])
def test_refund_rejects_non_positive_amount(service, amount):
    with pytest.raises(ValueError):
        service.refund("cust_42", amount)


def test_refund_rejects_blank_customer(service):
    with pytest.raises(ValueError):
        service.refund("", 100)


def test_refund_is_recorded_as_refund_kind(service, gateway):
    service.refund("cust_7", 100)
    assert [r.kind for r in gateway.receipts] == ["refund"]
