# buggy_payment_app (RepoDoctor golden fixture)

INTENTIONALLY BROKEN. Do not fix it by hand.

Contract:
- `pytest` -> 32 tests: 31 pass, 1 fails
- failing test: tests/test_payment.py::test_charge_records_receipt_for_customer
- root cause: PaymentService.charge() passes undefined `user_id` instead of `customer_id`
- expected fix: ../expected_patches/buggy_payment_app.diff (one file, one line)
