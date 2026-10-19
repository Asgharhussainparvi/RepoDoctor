"""Orchestrator + tools + FakeModel on the trusted fixture (real git and pytest, no Docker, no AWS)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import BUG_LINE, FAILING_TEST, FIXTURE
from repodoctor.agent import (
    AgentConfig,
    FakeModel,
    InvalidTransition,
    ModelUnavailableError,
    Orchestrator,
    OrchestratorError,
    SessionStore,
    State,
)
from repodoctor.agent.verifier import Verifier
from repodoctor.sandbox.errors import DockerUnavailableError
from repodoctor.tools import ToolLimits
from repodoctor.tools.models import Counts, RunTestsOutput


def payment_source(sandbox) -> str:
    return (Path(sandbox.workspace) / "app" / "payment.py").read_text()


def messages(orch, sid):
    return [e.message for e in orch.events(sid)]


# ------------------------------------------------------------------ happy path
def test_happy_path_requires_approval_then_verifies(
    make_orchestrator, sandbox_factory, expected_diff
):
    model = FakeModel([expected_diff])
    orch = make_orchestrator(model)
    sid = orch.create_session(FIXTURE)

    waiting = orch.start(sid)
    assert (
        waiting.state is State.WAITING_APPROVAL
        and waiting.ui_state == "awaiting approval"
    )
    assert waiting.report is None
    pending = waiting.pending_patch
    assert (
        pending.files == ["app/payment.py"]
        and "+        return self._gateway.charge(customer_id" in pending.diff
    )
    assert "user_id" in payment_source(
        sandbox_factory.created[0]
    )  # NOT applied before approval
    assert not sandbox_factory.created[0].destroyed  # sandbox kept while waiting

    done = orch.approve(sid, pending.sha256)
    assert done.state is State.SUCCESS and done.ui_state == "success"
    assert "user_id" not in payment_source(sandbox_factory.created[0])
    assert sandbox_factory.created[0].destroyed and len(sandbox_factory.created) == 1

    report = done.report
    assert report.headline.startswith("DEBUGGING COMPLETE")
    assert (report.before.failed, report.before.passed) == (1, 31)
    assert (report.after.failed, report.after.passed) == (0, 32)
    assert (
        report.failing_tests_before == [FAILING_TEST]
        and report.failing_tests_after == []
    )
    assert (
        report.files_changed == ["app/payment.py"]
        and report.patch_sha256 == pending.sha256
    )
    assert (
        report.root_cause.file == "app/payment.py"
        and report.root_cause.line == BUG_LINE
    )
    assert report.verification.verified and report.verification.authentic()
    assert report.patch_attempts == 1


def test_timeline_is_high_level_and_ordered(make_orchestrator, expected_diff):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)
    orch.approve(sid, orch.start(sid).pending_patch.sha256)
    states = [e.state for e in orch.events(sid) if e.kind == "state"]
    assert states == [
        State.ANALYZE_REPOSITORY,
        State.RUN_TESTS,
        State.ANALYZE_FAILURE,
        State.INVESTIGATE,
        State.FORM_HYPOTHESIS,
        State.GENERATE_PATCH,
        State.WAITING_APPROVAL,
        State.APPLY_PATCH,
        State.VERIFY,
        State.SUCCESS,
    ]
    text = " | ".join(messages(orch, sid))
    for expected in (
        "Reproduced failure: 1 failed, 31 passed",
        "Located failing test",
        f"Identified root cause in app/payment.py:{BUG_LINE}",
        "Waiting for your approval",
        "Verification passed",
    ):
        assert expected in text


def test_tool_calls_are_recorded_and_agent_budget_is_separate(
    make_orchestrator, expected_diff
):
    store = SessionStore()
    orch = make_orchestrator(FakeModel([expected_diff]), store=store)
    sid = orch.create_session(FIXTURE)
    orch.approve(sid, orch.start(sid).pending_patch.sha256)
    calls = store.tool_calls(sid)
    tools = [c["tool"] for c in calls]
    assert {
        "load_repository",
        "list_files",
        "run_tests",
        "search_code",
        "read_file",
        "check_patch",
        "apply_patch",
        "git_diff",
        "run_tests",
    } <= set(tools)
    budgeted = [c["tool"] for c in calls if c["budgeted"]]
    assert budgeted == ["search_code", "read_file"]  # only the engine's own calls count


# ------------------------------------------------------- approval is enforced
def test_wrong_hash_is_refused_and_nothing_changes(
    make_orchestrator, sandbox_factory, expected_diff
):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)
    orch.start(sid)
    with pytest.raises(OrchestratorError) as err:
        orch.approve(sid, "f" * 64)
    assert err.value.code == "patch_mismatch"
    assert orch.get(sid).state is State.WAITING_APPROVAL
    assert "user_id" in payment_source(sandbox_factory.created[0])


def test_cannot_approve_before_start_or_twice_or_after_finish(
    make_orchestrator, expected_diff
):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)
    with pytest.raises(OrchestratorError) as early:
        orch.approve(sid, "a" * 64)
    assert early.value.code == "invalid_state"
    sha = orch.start(sid).pending_patch.sha256
    orch.approve(sid, sha)
    with pytest.raises(OrchestratorError) as late:
        orch.approve(sid, sha)
    assert late.value.code == "invalid_state"


def test_cannot_start_twice_or_on_unknown_session(make_orchestrator, expected_diff):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)
    orch.start(sid)
    with pytest.raises(OrchestratorError, match="already started"):
        orch.start(sid)
    with pytest.raises(OrchestratorError) as err:
        orch.get("nope")
    assert err.value.code == "session_not_found"


def test_success_cannot_be_reached_without_a_sealed_verified_result(
    make_orchestrator, expected_diff
):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)
    orch.start(sid)
    with pytest.raises(
        InvalidTransition
    ):  # not even a legal edge from WAITING_APPROVAL
        orch._transition(sid, State.SUCCESS)
    real_fail = Verifier.verify(
        before=RunTestsOutput(status="failed", counts=Counts(passed=31, failed=1)),
        after=RunTestsOutput(status="failed", counts=Counts(passed=31, failed=1)),
        patch_sha256="a" * 64,
        files_changed=[],
    )
    forged = real_fail.model_copy(update={"verified": True, "reason_code": "verified"})
    for bad in (None, real_fail, forged):
        orch._store.set_state(
            sid, expected=State.WAITING_APPROVAL, new=State.VERIFY
        )  # test shortcut
        with pytest.raises(InvalidTransition):
            orch._transition(sid, State.SUCCESS, verification=bad)
        orch._store.set_state(sid, expected=State.VERIFY, new=State.WAITING_APPROVAL)
    assert orch.get(sid).state is State.WAITING_APPROVAL


# ---------------------------------------------------------- reject / cancel
def test_reject_never_applies_the_patch(
    make_orchestrator, sandbox_factory, expected_diff
):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)
    sha = orch.start(sid).pending_patch.sha256
    snap = orch.reject(sid, sha)
    assert snap.state is State.REJECTED and snap.ui_state == "cancelled"
    assert "user_id" in payment_source(sandbox_factory.created[0])
    assert sandbox_factory.created[0].destroyed
    assert snap.report.after is None and snap.report.verification is None
    with pytest.raises(OrchestratorError):
        orch.approve(sid, sha)


def test_cancel_while_waiting_cleans_up(
    make_orchestrator, sandbox_factory, expected_diff
):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)
    orch.start(sid)
    assert orch.cancel(sid).state is State.CANCELLED
    assert sandbox_factory.created[0].destroyed
    with pytest.raises(OrchestratorError):
        orch.cancel(sid)


def test_cancel_during_investigation_stops_the_run(make_orchestrator, sandbox_factory):
    holder = {}

    class CancellingModel(FakeModel):
        def investigate(self, context, tools):
            holder["orch"].cancel(context.session_id)
            return super().investigate(context, tools)

    orch = make_orchestrator(CancellingModel())
    holder["orch"] = orch
    sid = orch.create_session(FIXTURE)
    snap = orch.start(sid)
    assert snap.state is State.CANCELLED and snap.pending_patch is None
    assert sandbox_factory.created[0].destroyed


# ------------------------------------------------------------- retry logic
def test_wrong_patch_is_reported_then_a_second_approved_patch_succeeds(
    make_orchestrator, sandbox_factory, wrong_diff, expected_diff
):
    model = FakeModel([wrong_diff, expected_diff])
    orch = make_orchestrator(model)
    sid = orch.create_session(FIXTURE)

    first = orch.start(sid).pending_patch
    second_wait = orch.approve(
        sid, first.sha256
    )  # applied, verified, NOT a success -> retry -> new proposal
    assert (
        second_wait.state is State.WAITING_APPROVAL and second_wait.patch_attempts == 2
    )
    assert second_wait.pending_patch.sha256 != first.sha256
    assert "user_id" in payment_source(
        sandbox_factory.created[0]
    )  # workspace was reset between attempts
    assert [c.reason_code for c in model.patch_contexts[1].previous_attempts] == [
        "tests_still_failing"
    ]
    assert (
        "will not claim success" in model.patch_contexts[1].previous_attempts[0].message
    )

    done = orch.approve(
        sid, second_wait.pending_patch.sha256
    )  # a NEW approval was required
    assert done.state is State.SUCCESS and done.report.patch_attempts == 2
    assert len(sandbox_factory.created) == 1  # one sandbox for the whole session


def test_three_wrong_patches_end_in_an_honest_failure(make_orchestrator, wrong_diff):
    variants = [
        wrong_diff.replace("other_id", n) for n in ("other_id", "second_id", "third_id")
    ]
    orch = make_orchestrator(FakeModel(variants))
    sid = orch.create_session(FIXTURE)
    snap = orch.start(sid)
    for _ in range(3):
        assert snap.state is State.WAITING_APPROVAL
        snap = orch.approve(sid, snap.pending_patch.sha256)
    assert (
        snap.state is State.FAILED
        and snap.message == "I could not safely verify a fix."
    )
    assert snap.error_code == "attempts_exhausted" and snap.ui_state == "failed"
    assert (
        snap.report.verification is not None and not snap.report.verification.verified
    )
    assert snap.report.headline == "I could not safely verify a fix."
    assert (
        "DEBUGGING COMPLETE" not in snap.report.headline
        and snap.report.patch_attempts == 3
    )
    assert [f.reason_code for f in snap.report.attempt_feedback] == [
        "tests_still_failing"
    ] * 3


def test_unsafe_patches_consume_attempts_and_are_never_offered(
    make_orchestrator, expected_diff
):
    protected = expected_diff.replace("app/payment.py", "tests/test_payment.py")
    stale = expected_diff.replace("charge(user_id", "charge(nothing_like_this")
    model = FakeModel([protected, stale, expected_diff])
    orch = make_orchestrator(model)
    sid = orch.create_session(FIXTURE)
    snap = orch.start(sid)
    assert (
        snap.state is State.WAITING_APPROVAL and snap.patch_attempts == 3
    )  # only the 3rd was good
    assert [f.reason_code for f in model.patch_contexts[2].previous_attempts] == [
        "protected_path",
        "patch_does_not_apply",
    ]
    assert sum("could not be safely applied" in m for m in messages(orch, sid)) == 2


def test_all_unsafe_patches_fail_the_session(make_orchestrator, expected_diff):
    protected = expected_diff.replace("app/payment.py", "tests/test_payment.py")
    orch = make_orchestrator(FakeModel([protected] * 3))
    snap = orch.start(orch.create_session(FIXTURE))
    assert (
        snap.state is State.FAILED
        and snap.message == "I could not safely verify a fix."
    )


def test_duplicate_patch_is_not_offered_twice(
    make_orchestrator, wrong_diff, expected_diff
):
    model = FakeModel([wrong_diff, wrong_diff, expected_diff])
    orch = make_orchestrator(model)
    sid = orch.create_session(FIXTURE)
    snap = orch.approve(sid, orch.start(sid).pending_patch.sha256)
    assert snap.state is State.WAITING_APPROVAL and snap.patch_attempts == 3
    assert [f.reason_code for f in model.patch_contexts[2].previous_attempts] == [
        "tests_still_failing",
        "duplicate_patch",
    ]


def test_engine_producing_no_patch_consumes_attempts(make_orchestrator):
    from repodoctor.agent import PatchGenerationError

    orch = make_orchestrator(FakeModel([PatchGenerationError("empty answer")] * 3))
    snap = orch.start(orch.create_session(FIXTURE))
    assert snap.state is State.FAILED and snap.error_code == "attempts_exhausted"


def test_attempt_limit_is_configurable(make_orchestrator, wrong_diff):
    orch = make_orchestrator(
        FakeModel([wrong_diff]), config=AgentConfig(max_patch_attempts=1)
    )
    sid = orch.create_session(FIXTURE)
    snap = orch.approve(sid, orch.start(sid).pending_patch.sha256)
    assert snap.state is State.FAILED and snap.patch_attempts == 1


# ---------------------------------------------------------- non-debuggable inputs
def _fixed_copy(tmp_path, expected_diff) -> Path:
    repo = tmp_path / "already_fixed"
    shutil.copytree(
        FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )
    source = repo / "app" / "payment.py"
    source.write_text(
        source.read_text().replace("charge(user_id", "charge(customer_id")
    )
    return repo


def test_passing_repo_needs_no_debugging(make_orchestrator, tmp_path, expected_diff):
    model = FakeModel([expected_diff])
    orch = make_orchestrator(model)
    snap = orch.start(orch.create_session(_fixed_copy(tmp_path, expected_diff)))
    assert snap.state is State.NO_FAILURE and snap.ui_state == "success"
    assert "no failure to debug" in snap.message and model.investigate_calls == 0
    assert (
        snap.report.headline
        != "DEBUGGING COMPLETE: fix verified by re-running the tests."
    )


def test_invalid_project_error_message(make_orchestrator, tmp_path):
    repo = tmp_path / "docs_only"
    repo.mkdir()
    (repo / "README.md").write_text("# nothing to run\n")
    orch = make_orchestrator(FakeModel())
    snap = orch.start(orch.create_session(repo))
    assert snap.error_code == "invalid_project" and "no .py files" in snap.message


def test_empty_and_missing_repositories_fail_with_a_clear_error(
    make_orchestrator, tmp_path
):
    (tmp_path / "empty").mkdir()
    orch = make_orchestrator(FakeModel())
    empty = orch.start(orch.create_session(tmp_path / "empty"))
    assert (
        empty.state is State.FAILED
        and empty.error_code == "upload_rejected"
        and "no files" in empty.message
    )
    missing = orch.start(orch.create_session(tmp_path / "does-not-exist"))
    assert missing.state is State.FAILED and missing.error_code == "upload_rejected"


def test_test_timeout_is_reported_with_the_standard_message(
    make_orchestrator, tmp_path, sandbox_factory
):
    repo = tmp_path / "slow"
    shutil.copytree(
        FIXTURE, repo, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )
    (repo / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(30)\n"
    )
    orch = make_orchestrator(FakeModel(), config=AgentConfig(test_timeout_seconds=2))
    snap = orch.start(orch.create_session(repo))
    assert (
        snap.state is State.FAILED
        and snap.error_code == "test_timeout"
        and snap.ui_state == "timeout"
    )
    assert snap.message == "Test execution exceeded the allowed time."
    assert sandbox_factory.created[0].destroyed


# ------------------------------------------------------------ failure handling
def test_ai_unavailable_is_a_clean_failure(make_orchestrator, sandbox_factory):
    orch = make_orchestrator(
        FakeModel(investigate_error=ModelUnavailableError("no credentials"))
    )
    snap = orch.start(orch.create_session(FIXTURE))
    assert snap.state is State.FAILED and snap.error_code == "ai_unavailable"
    assert snap.message == "AI service unavailable. Check AWS configuration."
    assert sandbox_factory.created[0].destroyed


def test_ai_unavailable_while_proposing_a_patch(make_orchestrator):
    orch = make_orchestrator(FakeModel([ModelUnavailableError("throttled")]))
    snap = orch.start(orch.create_session(FIXTURE))
    assert snap.error_code == "ai_unavailable"


def test_docker_unavailable_is_a_clean_failure(make_orchestrator):
    def broken_factory():
        raise DockerUnavailableError("Sandbox unavailable. Docker must be running.")

    orch = make_orchestrator(FakeModel(), factory=broken_factory)
    snap = orch.start(orch.create_session(FIXTURE))
    assert snap.state is State.FAILED and snap.error_code == "sandbox_unavailable"
    assert "Docker must be running" in snap.message


def test_sandbox_start_failure_destroys_the_half_built_sandbox(
    make_orchestrator, sandbox_factory
):
    def failing_start():
        sandbox = sandbox_factory()
        sandbox.start = lambda: (_ for _ in ()).throw(
            DockerUnavailableError("Sandbox unavailable. Docker must be running.")
        )
        return sandbox

    orch = make_orchestrator(FakeModel(), factory=failing_start)
    assert orch.start(orch.create_session(FIXTURE)).error_code == "sandbox_unavailable"
    assert sandbox_factory.created[0].destroyed


def test_engine_crash_is_contained(make_orchestrator):
    orch = make_orchestrator(
        FakeModel(investigate_error=RuntimeError("bug in the model adapter"))
    )
    snap = orch.start(orch.create_session(FIXTURE))
    assert snap.state is State.FAILED and snap.error_code == "agent_error"


def test_hypothesis_about_a_missing_file_is_refused(make_orchestrator):
    from repodoctor.agent import Hypothesis

    orch = make_orchestrator(
        FakeModel(hypothesis=Hypothesis(file="app/ghost.py", description="x"))
    )
    assert orch.start(orch.create_session(FIXTURE)).error_code == "invalid_hypothesis"


def test_investigation_failure_is_reported(make_orchestrator):
    orch = make_orchestrator(
        FakeModel(
            investigate_error=__import__(
                "repodoctor.agent", fromlist=["x"]
            ).InvestigationFailed("no idea")
        )
    )
    snap = orch.start(orch.create_session(FIXTURE))
    assert (
        snap.error_code == "investigation_failed"
        and "could not identify the root cause" in snap.message
    )


def test_tool_budget_does_not_starve_the_orchestrator(make_orchestrator):
    orch = make_orchestrator(FakeModel(), tool_limits=ToolLimits(max_tool_calls=0))
    sid = orch.create_session(FIXTURE)
    snap = orch.start(sid)
    assert snap.error_code == "investigation_failed"  # the engine had no tools...
    assert orch._store.get_session(
        sid
    ).reproduction_json  # ...but the orchestrator still ran the tests


def test_session_deadline(make_orchestrator):
    ticks = iter(range(0, 10_000, 100))
    orch = make_orchestrator(
        FakeModel(),
        config=AgentConfig(max_active_seconds=150),
        clock=lambda: float(next(ticks)),
    )
    snap = orch.start(orch.create_session(FIXTURE))
    assert (
        snap.state is State.FAILED
        and snap.error_code == "session_timeout"
        and snap.ui_state == "timeout"
    )


# ---------------------------------------------------------------- persistence
def test_waiting_approval_survives_a_restart(
    tmp_path, make_orchestrator, expected_diff
):
    from conftest import SandboxFactory

    db = tmp_path / "sessions.sqlite3"

    store_a = SessionStore(db)
    orch_a = make_orchestrator(FakeModel([expected_diff]), store=store_a)
    sid = orch_a.create_session(FIXTURE)
    sha = orch_a.start(sid).pending_patch.sha256
    store_a.close()  # --- "server restarts": all in-memory state is gone ---

    factory_b = SandboxFactory(tmp_path / "after_restart")
    engine_b = FakeModel()
    orch_b = make_orchestrator(engine_b, store=SessionStore(db), factory=factory_b)
    waiting = orch_b.get(sid)
    assert (
        waiting.state is State.WAITING_APPROVAL and waiting.pending_patch.sha256 == sha
    )

    with pytest.raises(OrchestratorError):  # still enforced after the restart
        orch_b.approve(sid, "0" * 64)
    done = orch_b.approve(sid, sha)
    assert done.state is State.SUCCESS and done.report.verification.authentic()
    assert (done.report.before.failed, done.report.after.passed) == (1, 32)
    assert (
        engine_b.investigate_calls == 0
        and len(factory_b.created) == 1
        and factory_b.created[0].destroyed
    )
