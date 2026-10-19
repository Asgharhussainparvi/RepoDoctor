"""The debugging state machine: states, allowed transitions, UI mapping. Pure data."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    INITIALIZE = "INITIALIZE"
    ANALYZE_REPOSITORY = "ANALYZE_REPOSITORY"
    RUN_TESTS = "RUN_TESTS"
    ANALYZE_FAILURE = "ANALYZE_FAILURE"
    INVESTIGATE = "INVESTIGATE"
    FORM_HYPOTHESIS = "FORM_HYPOTHESIS"
    GENERATE_PATCH = "GENERATE_PATCH"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPLY_PATCH = "APPLY_PATCH"
    VERIFY = "VERIFY"
    RETRY = "RETRY"
    # terminal
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    NO_FAILURE = "NO_FAILURE"


S = State
TERMINAL_STATES = frozenset(
    {S.SUCCESS, S.FAILED, S.REJECTED, S.CANCELLED, S.NO_FAILURE}
)
_ABORT = {S.FAILED, S.CANCELLED}

TRANSITIONS: dict[State, frozenset[State]] = {
    S.INITIALIZE: frozenset({S.ANALYZE_REPOSITORY} | _ABORT),
    S.ANALYZE_REPOSITORY: frozenset({S.RUN_TESTS} | _ABORT),
    S.RUN_TESTS: frozenset({S.ANALYZE_FAILURE, S.NO_FAILURE} | _ABORT),
    S.ANALYZE_FAILURE: frozenset({S.INVESTIGATE} | _ABORT),
    S.INVESTIGATE: frozenset({S.FORM_HYPOTHESIS} | _ABORT),
    S.FORM_HYPOTHESIS: frozenset({S.GENERATE_PATCH} | _ABORT),
    S.GENERATE_PATCH: frozenset({S.WAITING_APPROVAL} | _ABORT),
    S.WAITING_APPROVAL: frozenset({S.APPLY_PATCH, S.REJECTED} | _ABORT),
    S.APPLY_PATCH: frozenset({S.VERIFY} | _ABORT),
    S.VERIFY: frozenset({S.SUCCESS, S.RETRY} | _ABORT),
    S.RETRY: frozenset({S.GENERATE_PATCH} | _ABORT),
    **{state: frozenset() for state in TERMINAL_STATES},
}


class InvalidTransition(Exception):
    """A state change that the machine does not allow."""


def assert_transition(current: State, new: State) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransition(f"{current.value} -> {new.value} is not allowed")


# Labels for the UI (Phase 7). `timeout` is derived from the error code.
_UI = {
    S.INITIALIZE: "analyzing",
    S.ANALYZE_REPOSITORY: "analyzing",
    S.RUN_TESTS: "running tests",
    S.ANALYZE_FAILURE: "investigating",
    S.INVESTIGATE: "investigating",
    S.FORM_HYPOTHESIS: "investigating",
    S.GENERATE_PATCH: "patch proposed",
    S.WAITING_APPROVAL: "awaiting approval",
    S.APPLY_PATCH: "applying patch",
    S.VERIFY: "verifying",
    S.RETRY: "investigating",
    S.SUCCESS: "success",
    S.FAILED: "failed",
    S.REJECTED: "cancelled",
    S.CANCELLED: "cancelled",
    S.NO_FAILURE: "success",
}
TIMEOUT_CODES = frozenset({"test_timeout", "session_timeout"})


def ui_state(state: State, error_code: str | None = None) -> str:
    if state is S.FAILED and error_code in TIMEOUT_CODES:
        return "timeout"
    return _UI[state]
