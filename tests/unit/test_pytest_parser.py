import pytest

from repodoctor.tools.pytest_parser import classify_run, parse_pytest_output

FAILED = """\
.....................F..........
=================================== FAILURES ===================================
___________________ test_charge_records_receipt_for_customer ___________________
app/payment.py:22: in charge
    return self._gateway.charge(user_id, amount_cents)
E   NameError: name 'user_id' is not defined
=========================== short test summary info ============================
FAILED tests/test_payment.py::test_charge_records_receipt_for_customer - NameError: name 'user_id' is not defined
1 failed, 31 passed in 1.95s
"""


def test_parses_failed_run():
    run = parse_pytest_output(FAILED)
    assert (run.counts.failed, run.counts.passed) == (1, 31)
    assert run.duration_seconds == 1.95
    assert [f.test_id for f in run.failures] == [
        "tests/test_payment.py::test_charge_records_receipt_for_customer"
    ]
    assert "NameError" in run.failures[0].message
    assert classify_run(1, run, False) == "failed"


def test_parses_all_passed_with_decorated_line():
    run = parse_pytest_output(
        "....\n============================== 32 passed in 0.50s ==============================\n"
    )
    assert run.counts.passed == 32 and classify_run(0, run, False) == "passed"


def test_parses_mixed_counts_and_duration_suffix():
    run = parse_pytest_output(
        "2 failed, 3 passed, 1 skipped, 2 warnings, 1 error in 61.20s (0:01:01)\n"
    )
    c = run.counts
    assert (c.failed, c.passed, c.skipped, c.warnings, c.errors) == (2, 3, 1, 2, 1)


def test_no_tests_ran():
    run = parse_pytest_output("no tests ran in 0.01s\n")
    assert run.counts.passed == 0 and classify_run(5, run, False) == "no_tests"


def test_collection_error_is_error_status():
    run = parse_pytest_output(
        "ERROR tests/test_x.py - ImportError: boom\n1 error in 0.12s\n"
    )
    assert run.counts.errors == 1 and classify_run(2, run, False) == "error"


def test_param_ids_with_spaces_and_dedupe():
    out = (
        "=== short test summary info ===\n"
        "FAILED t.py::test_a[a b] - assert 1 == 2\n"
        "FAILED t.py::test_a[a b] - assert 1 == 2\n"
        "ERROR t.py::test_b\n"
        "1 failed, 1 error in 0.1s\n"
    )
    run = parse_pytest_output(out)
    assert [(f.test_id, f.kind) for f in run.failures] == [
        ("t.py::test_a[a b]", "FAILED"),
        ("t.py::test_b", "ERROR"),
    ]


def test_last_summary_line_wins():
    run = parse_pytest_output("99 passed in 0.1s\n1 failed, 2 passed in 0.2s\n")
    assert (run.counts.failed, run.counts.passed) == (1, 2)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello world",
        "Traceback...\nModuleNotFoundError: pytest\n",
        "took 5 seconds\n",
        "3 bananas in 2.0s\n",
    ],
)
def test_garbage_is_unparseable(text):
    assert parse_pytest_output(text) is None
    assert classify_run(1, None, False) == "unparseable"


@pytest.mark.parametrize(
    ("exit_code", "summary", "timed_out", "expected"),
    [
        (0, "5 passed in 1s", False, "passed"),
        (0, "5 skipped in 1s", False, "no_tests"),  # nothing actually verified
        (
            0,
            "1 failed, 4 passed in 1s",
            False,
            "unparseable",
        ),  # exit code and counts disagree
        (1, "5 passed in 1s", False, "unparseable"),
        (1, "1 failed in 1s", False, "failed"),
        (1, "1 error, 4 passed in 1s", False, "failed"),
        (2, "1 error in 1s", False, "error"),
        (3, "1 passed in 1s", False, "error"),
        (4, "1 passed in 1s", False, "error"),
        (5, "no tests ran in 1s", False, "no_tests"),
        (None, "1 passed in 1s", False, "unparseable"),
        (0, "5 passed in 1s", True, "timeout"),
    ],
)
def test_classification_matrix(exit_code, summary, timed_out, expected):
    assert (
        classify_run(exit_code, parse_pytest_output(summary + "\n"), timed_out)
        == expected
    )


def test_only_a_clean_pass_is_passed():
    outcomes = {
        classify_run(code, parse_pytest_output(s + "\n"), False)
        for code in (0, 1, 2, 3, 4, 5, 137)
        for s in (
            "1 failed in 1s",
            "1 error in 1s",
            "no tests ran in 1s",
            "3 skipped in 1s",
        )
    }
    assert "passed" not in outcomes
