import pytest

from repodoctor.agent.schemas import AttemptFeedback
from repodoctor.agent.states import State
from repodoctor.agent.store import SessionStore, StaleStateError
from repodoctor.tools.toolbox import ToolCallRecord

SHA = "c" * 64


@pytest.fixture
def store():
    return SessionStore()


@pytest.fixture
def sid(store):
    return store.create_session("/repo")


def test_create_and_get(store, sid):
    row = store.get_session(sid)
    assert row.state is State.INITIALIZE and row.repo_path == "/repo" and row.patch_attempts == 0
    with pytest.raises(KeyError):
        store.get_session("nope")


def test_set_state_is_compare_and_set(store, sid):
    store.set_state(sid, expected=State.INITIALIZE, new=State.RUN_TESTS, message="m")
    assert store.get_session(sid).state is State.RUN_TESTS
    with pytest.raises(StaleStateError):
        store.set_state(sid, expected=State.INITIALIZE, new=State.FAILED)
    assert store.get_session(sid).state is State.RUN_TESTS


def test_steps_are_ordered_and_typed(store, sid):
    store.add_step(sid, State.INITIALIZE, "info", "one")
    store.add_step(sid, State.RUN_TESTS, "state", "two")
    steps = store.steps(sid)
    assert [(s.seq < steps[-1].seq or s is steps[-1], s.message) for s in steps] == [(True, "one"), (True, "two")]
    assert steps[1].state is State.RUN_TESTS


def test_counters_feedback_and_json_fields(store, sid):
    assert store.increment_attempts(sid) == 1 and store.increment_attempts(sid) == 2
    store.add_active_seconds(sid, 2.5)
    store.add_active_seconds(sid, -9)  # negative time is ignored
    store.append_feedback(sid, AttemptFeedback(attempt=1, reason_code="x", message="y"))
    store.save_reproduction(sid, "{}")
    row = store.get_session(sid)
    assert row.patch_attempts == 2 and row.active_seconds == 2.5 and row.reproduction_json == "{}"
    assert [(f.attempt, f.reason_code) for f in row.feedback] == [(1, "x")]


def test_tool_calls_round_trip(store, sid):
    store.add_tool_calls(sid, [ToolCallRecord("read_file", True, None, 0.1, True),
                               ToolCallRecord("run_tests", False, "timeout", 2.0, False)])
    calls = store.tool_calls(sid)
    assert [(c["tool"], c["ok"], c["budgeted"]) for c in calls] == [("read_file", 1, 1), ("run_tests", 0, 0)]


def add_patch(store, sid, sha=SHA):
    store.add_patch(sid, attempt=1, sha256=sha, diff="d", summary="s", files=["a.py"])


def test_patch_lifecycle_and_uniqueness(store, sid):
    add_patch(store, sid)
    assert store.has_patch(sid, SHA) and store.get_patch(sid, SHA).status == "proposed"
    with pytest.raises(Exception):
        add_patch(store, sid)  # same hash twice in one session
    store.set_patch_status(sid, SHA, expected="proposed", new="applied")
    with pytest.raises(StaleStateError):
        store.set_patch_status(sid, SHA, expected="proposed", new="applied")
    assert store.latest_patch(sid, "applied").files == ["a.py"] and store.latest_patch(sid, "proposed") is None


def test_approve_requires_waiting_state_and_pending_patch(store, sid):
    add_patch(store, sid)
    with pytest.raises(StaleStateError):  # session is still INITIALIZE
        store.approve_patch(sid, SHA)
    store.set_state(sid, expected=State.INITIALIZE, new=State.WAITING_APPROVAL)  # (test shortcut)
    with pytest.raises(KeyError):
        store.approve_patch(sid, "d" * 64)
    assert store.get_session(sid).state is State.WAITING_APPROVAL  # failed approval rolled everything back
    store.approve_patch(sid, SHA)
    assert store.get_session(sid).state is State.APPLY_PATCH
    assert store.get_patch(sid, SHA).status == "approved"
    with pytest.raises(StaleStateError):  # a second approval is impossible
        store.approve_patch(sid, SHA)


def test_reject_marks_patch_and_state(store, sid):
    add_patch(store, sid)
    store.set_state(sid, expected=State.INITIALIZE, new=State.WAITING_APPROVAL)
    store.reject_patch(sid, SHA)
    assert store.get_session(sid).state is State.REJECTED and store.get_patch(sid, SHA).status == "rejected"


def test_data_survives_reopening_a_file_database(tmp_path):
    path = tmp_path / "rd.sqlite3"
    first = SessionStore(path)
    sid = first.create_session("/repo")
    add_patch(first, sid)
    first.set_state(sid, expected=State.INITIALIZE, new=State.WAITING_APPROVAL, message="waiting")
    first.close()
    second = SessionStore(path)
    assert second.get_session(sid).state is State.WAITING_APPROVAL
    assert second.get_patch(sid, SHA).diff == "d"
