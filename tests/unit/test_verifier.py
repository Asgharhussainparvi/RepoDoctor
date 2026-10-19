import pytest

from repodoctor.agent.verifier import Verifier
from repodoctor.tools.models import Counts, FailedTest, RunTestsOutput

SHA = "a" * 64


def run(status, *, passed=0, failed=0, errors=0, skipped=0, failing=(), ok=True):
    return RunTestsOutput(
        ok=ok,
        status=status,
        counts=Counts(passed=passed, failed=failed, errors=errors, skipped=skipped),
        failed_tests=[FailedTest(test_id=t) for t in failing],
    )


BEFORE = run("failed", passed=31, failed=1, failing=["t.py::a"])


def verify(after, before=BEFORE):
    return Verifier.verify(
        before=before, after=after, patch_sha256=SHA, files_changed=["app/x.py"]
    )


def test_clean_pass_is_verified_and_sealed():
    r = verify(run("passed", passed=32))
    assert r.verified and r.reason_code == "verified" and r.authentic()
    assert (
        r.previously_failing == ["t.py::a"]
        and r.still_failing == []
        and r.new_failures == []
    )
    assert (r.before.failed, r.after.passed) == (1, 32)


@pytest.mark.parametrize(
    ("after", "reason"),
    [
        (
            run("failed", passed=31, failed=1, failing=["t.py::a"]),
            "tests_still_failing",
        ),
        (run("failed", passed=31, failed=1, failing=["t.py::b"]), "new_failures"),
        (run("timeout"), "tests_timed_out"),
        (run("unparseable"), "test_output_unusable"),
        (run("error", errors=1), "test_output_unusable"),
        (run("no_tests"), "test_output_unusable"),
        (run("passed", ok=False), "test_run_failed"),
        (run("passed", passed=10), "test_count_decreased"),  # tests were removed
        (
            run("passed", passed=31, skipped=0),
            "test_count_decreased",
        ),  # 31 < 32 total before
    ],
)
def test_everything_else_is_not_verified(after, reason):
    r = verify(after)
    assert not r.verified and r.reason_code == reason and r.authentic()
    assert "will not claim success" in r.message or reason == "baseline_not_reproduced"


@pytest.mark.parametrize(
    "before",
    [run("passed", passed=32), run("unparseable"), run("failed", ok=False, failed=1)],
)
def test_unreproduced_baseline_can_never_verify(before):
    r = verify(run("passed", passed=32), before=before)
    assert not r.verified and r.reason_code == "baseline_not_reproduced"


def test_a_forged_result_is_not_authentic():
    real = verify(run("passed", passed=32))
    forged = real.model_copy(update={"seal": "0" * 64})
    assert not forged.authentic()
    assert not real.model_copy(update={"seal": ""}).authentic()


def test_any_tampering_breaks_the_seal():
    real = verify(run("failed", passed=31, failed=1, failing=["t.py::a"]))
    assert real.authentic() and not real.verified
    flipped = real.model_copy(update={"verified": True, "reason_code": "verified"})
    assert not flipped.authentic()
    assert not real.model_copy(update={"patch_sha256": "b" * 64}).authentic()


def test_in_place_mutation_of_lists_is_detected():
    real = verify(run("passed", passed=32))
    real.files_changed.append("tests/test_x.py")
    assert not real.authentic()


def test_json_round_trip_keeps_the_seal_valid():
    from repodoctor.agent.verifier import VerificationResult

    real = verify(run("passed", passed=32))
    assert VerificationResult.model_validate_json(real.model_dump_json()).authentic()
