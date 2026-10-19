from repodoctor.agent.states import (
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransition,
    State,
    assert_transition,
    ui_state,
)
import pytest


def reachable(start, banned=frozenset()):
    seen, todo = set(), [start]
    while todo:
        state = todo.pop()
        if state in seen or state in banned:
            continue
        seen.add(state)
        todo.extend(TRANSITIONS[state])
    return seen


def test_every_state_has_a_transition_entry():
    assert set(TRANSITIONS) == set(State)


def test_terminal_states_have_no_exits():
    assert all(TRANSITIONS[s] == frozenset() for s in TERMINAL_STATES)
    assert TERMINAL_STATES == {
        State.SUCCESS,
        State.FAILED,
        State.REJECTED,
        State.CANCELLED,
        State.NO_FAILURE,
    }


def test_every_state_is_reachable_from_initialize():
    assert reachable(State.INITIALIZE) == set(State)


def test_success_is_only_reachable_from_verify():
    assert [s for s, outs in TRANSITIONS.items() if State.SUCCESS in outs] == [
        State.VERIFY
    ]


def test_apply_patch_is_only_reachable_from_waiting_approval():
    assert [s for s, outs in TRANSITIONS.items() if State.APPLY_PATCH in outs] == [
        State.WAITING_APPROVAL
    ]


def test_waiting_approval_is_only_entered_from_generate_patch():
    assert [s for s, outs in TRANSITIONS.items() if State.WAITING_APPROVAL in outs] == [
        State.GENERATE_PATCH
    ]


def test_no_path_to_apply_or_success_bypasses_the_approval_gate():
    without_gate = reachable(State.INITIALIZE, banned={State.WAITING_APPROVAL})
    assert State.APPLY_PATCH not in without_gate and State.VERIFY not in without_gate
    assert State.SUCCESS not in without_gate


def test_every_active_state_can_be_cancelled_or_failed():
    for state in set(State) - TERMINAL_STATES:
        assert {State.CANCELLED, State.FAILED} <= TRANSITIONS[state]


def test_assert_transition():
    assert_transition(State.VERIFY, State.RETRY)
    for bad in [
        (State.INITIALIZE, State.SUCCESS),
        (State.GENERATE_PATCH, State.APPLY_PATCH),
        (State.SUCCESS, State.INITIALIZE),
        (State.WAITING_APPROVAL, State.VERIFY),
    ]:
        with pytest.raises(InvalidTransition):
            assert_transition(*bad)


def test_ui_states_cover_everything_and_flag_timeouts():
    assert all(isinstance(ui_state(s), str) for s in State)
    assert ui_state(State.FAILED, "test_timeout") == "timeout"
    assert ui_state(State.FAILED, "session_timeout") == "timeout"
    assert ui_state(State.FAILED, "ai_unavailable") == "failed"
    assert ui_state(State.WAITING_APPROVAL) == "awaiting approval"
    assert ui_state(State.SUCCESS) == "success"
