"""Orchestrator features a long-running server needs: split approval, expiry, recovery, shutdown."""

import pytest

from conftest import FIXTURE
from repodoctor.agent import FakeModel, OrchestratorError, SessionStore, State


def waiting(make_orchestrator, expected_diff, **kwargs):
    orch = make_orchestrator(FakeModel([expected_diff]), **kwargs)
    sid = orch.create_session(FIXTURE)
    return orch, sid, orch.start(sid).pending_patch.sha256


def test_approval_is_recorded_synchronously_then_the_session_continues(
    make_orchestrator, expected_diff
):
    orch, sid, sha = waiting(make_orchestrator, expected_diff)
    orch.record_approval(sid, sha)
    assert orch.get(sid).state is State.APPLY_PATCH  # persisted before anything runs
    assert orch.continue_session(sid).state is State.SUCCESS


def test_record_approval_refuses_wrong_hash_and_second_attempt(
    make_orchestrator, expected_diff
):
    orch, sid, sha = waiting(make_orchestrator, expected_diff)
    with pytest.raises(OrchestratorError) as wrong:
        orch.record_approval(sid, "e" * 64)
    assert (
        wrong.value.code == "patch_mismatch"
        and orch.get(sid).state is State.WAITING_APPROVAL
    )
    orch.record_approval(sid, sha)
    with pytest.raises(OrchestratorError) as twice:
        orch.record_approval(sid, sha)
    assert twice.value.code == "invalid_state"


def test_cancel_accepts_a_custom_message(make_orchestrator, expected_diff):
    orch, sid, _ = waiting(make_orchestrator, expected_diff)
    assert orch.cancel(sid, "Because I said so.").message == "Because I said so."


def test_expiry_only_touches_old_waiting_sessions(
    make_orchestrator, expected_diff, sandbox_factory
):
    orch, sid, _ = waiting(make_orchestrator, expected_diff)
    assert orch.expire_stale_waiting(3600) == []  # not old enough
    assert orch.get(sid).state is State.WAITING_APPROVAL
    assert orch.expire_stale_waiting(0) == [sid]
    snap = orch.get(sid)
    assert snap.state is State.CANCELLED and "expired" in snap.message.lower()
    assert sandbox_factory.created[0].destroyed
    assert orch.expire_stale_waiting(0) == []  # terminal sessions are left alone


def test_recovery_fails_interrupted_sessions_but_keeps_waiting_ones(
    make_orchestrator, expected_diff
):
    store = SessionStore()
    orch, waiting_sid, _ = waiting(make_orchestrator, expected_diff, store=store)
    stuck = store.create_session(str(FIXTURE))
    store.set_state(stuck, expected=State.INITIALIZE, new=State.RUN_TESTS)
    fresh = store.create_session(
        str(FIXTURE)
    )  # INITIALIZE: never started, also interrupted by definition
    recovered = orch.recover_interrupted()
    assert set(recovered) == {stuck, fresh}
    assert (
        orch.get(stuck).error_code == "server_restarted"
        and orch.get(stuck).report is not None
    )
    assert orch.get(waiting_sid).state is State.WAITING_APPROVAL


def test_shutdown_destroys_sandboxes_but_a_later_approval_rebuilds_one(
    make_orchestrator, expected_diff, sandbox_factory
):
    orch, sid, sha = waiting(make_orchestrator, expected_diff)
    orch.shutdown()
    assert (
        sandbox_factory.created[0].destroyed
        and orch.get(sid).state is State.WAITING_APPROVAL
    )
    assert orch.approve(sid, sha).state is State.SUCCESS
    assert (
        len(sandbox_factory.created) == 2
    )  # a fresh sandbox was created from the stored patch


def test_a_finished_session_always_has_a_report_even_before_cleanup(
    make_orchestrator, expected_diff
):
    store = SessionStore()
    orch = make_orchestrator(FakeModel([expected_diff]), store=store)
    sid = store.create_session(str(FIXTURE))
    store.set_state(
        sid, expected=State.INITIALIZE, new=State.FAILED, message="boom", error_code="x"
    )  # no finalize yet
    assert store.get_session(sid).report_json is None
    snap = orch.get(sid)
    assert snap.report is not None and snap.report.headline == "boom"
    assert (
        store.get_session(sid).report_json is not None
    )  # stored, so later reads are identical
