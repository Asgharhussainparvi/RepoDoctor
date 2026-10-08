from app.gateway import FakeGateway


def test_gateway_assigns_sequential_receipt_ids():
    gateway = FakeGateway()
    first = gateway.charge("cust_1", 100)
    second = gateway.refund("cust_1", 50)
    assert (first.receipt_id, second.receipt_id) == ("rcpt_0001", "rcpt_0002")
