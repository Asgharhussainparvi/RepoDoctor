"""The Orchestrator: a deterministic state machine that owns every decision.

The reasoning engine proposes (a hypothesis, a patch). This module decides: it
enforces limits, runs the tools, requires a persisted human approval before any patch
is applied, and reaches SUCCESS only through a sealed Verifier result.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Protocol

from pydantic import ValidationError

from repodoctor.sandbox.errors import (
    DockerUnavailableError,
    SandboxError,
    SandboxImageMissingError,
    SandboxNotRunningError,
    UploadRejectedError,
)
from repodoctor.tools import Toolbox, ToolLimits, validate_repo_path
from repodoctor.tools.models import RunTestsOutput
from repodoctor.tools.toolbox import ExecutionBackend

from .reasoning import (
    AgentTools,
    InvestigationFailed,
    ModelUnavailableError,
    PatchGenerationError,
    ReasoningEngine,
)
from .schemas import (
    AgentConfig,
    AgentEvent,
    AttemptFeedback,
    DebugReport,
    Hypothesis,
    InvestigationContext,
    PatchContext,
    PatchView,
    RootCauseReport,
    SessionSnapshot,
)
from .states import (
    TERMINAL_STATES,
    InvalidTransition,
    State,
    assert_transition,
    ui_state,
)
from .store import PatchRow, SessionRow, SessionStore, StaleStateError
from .verifier import NOT_VERIFIED_MESSAGE, VerificationResult, Verifier

logger = logging.getLogger("repodoctor.agent")

AI_UNAVAILABLE = "AI service unavailable. Check AWS configuration."
TEST_TIMEOUT = "Test execution exceeded the allowed time."
PATCH_UNSAFE = "Proposed patch could not be safely applied."
GAVE_UP = "I could not safely verify a fix."


class SandboxHandle(ExecutionBackend, Protocol):
    def start(self) -> None: ...
    def destroy(self) -> None: ...


class OrchestratorError(Exception):
    """The caller did something the session does not allow (wrong state, bad hash...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Abort(Exception):
    """Internal: stop the run and move the session to FAILED."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Live:
    sandbox: SandboxHandle
    toolbox: Toolbox
    tools: AgentTools
    files: list[str] = field(default_factory=list)
    flushed: int = 0


_SANDBOX_CODES = (
    (DockerUnavailableError, "sandbox_unavailable"),
    (SandboxImageMissingError, "sandbox_image_missing"),
    (SandboxNotRunningError, "sandbox_not_running"),
    (UploadRejectedError, "upload_rejected"),
    (SandboxError, "sandbox_error"),
)


class Orchestrator:
    def __init__(
        self,
        store: SessionStore,
        engine: ReasoningEngine,
        sandbox_factory: Callable[[], SandboxHandle],
        *,
        config: AgentConfig | None = None,
        tool_limits: ToolLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._factory = sandbox_factory
        self._config = config or AgentConfig()
        self._tool_limits = tool_limits or ToolLimits()
        self._clock = clock
        self._on_event = on_event
        self._live: dict[str, _Live] = {}
        self._running: set[str] = set()
        self._guard = threading.Lock()

    # ================================================================ public API
    def create_session(self, repo_path: str | Path) -> str:
        sid = self._store.create_session(str(Path(repo_path).expanduser().resolve()))
        self._event(sid, State.INITIALIZE, "info", "Debugging session created.")
        return sid

    def start(self, sid: str) -> SessionSnapshot:
        with self._running_guard(sid):
            row = self._row(sid)
            if row.state is not State.INITIALIZE:
                raise OrchestratorError(
                    "invalid_state",
                    f"Session is {row.state.value}; it was already started.",
                )
            self._run(sid)
        return self.get(sid)

    def record_approval(self, sid: str, patch_sha256: str) -> None:
        """Synchronous half of approval: validate and persist it. Raises OrchestratorError if refused.

        After this the session is APPLY_PATCH; call continue_session() (possibly in a background thread).
        """
        row = self._row(sid)
        if row.state is not State.WAITING_APPROVAL:
            raise OrchestratorError(
                "invalid_state",
                f"Session is {row.state.value}, not waiting for approval.",
            )
        try:
            self._store.approve_patch(sid, patch_sha256)
        except KeyError as exc:
            raise OrchestratorError(
                "patch_mismatch", "No pending patch matches this hash."
            ) from exc
        except StaleStateError as exc:
            raise OrchestratorError(
                "invalid_state", "The session changed; approval was not recorded."
            ) from exc
        self._event(sid, State.APPLY_PATCH, "state", "Patch approved by user.")

    def continue_session(self, sid: str) -> SessionSnapshot:
        """Run a session forward from its current state (after approval)."""
        with self._running_guard(sid):
            self._run(sid)
        return self.get(sid)

    def approve(self, sid: str, patch_sha256: str) -> SessionSnapshot:
        """Human approval of ONE specific patch. Enforced here and again in the Toolbox."""
        self.record_approval(sid, patch_sha256)
        return self.continue_session(sid)

    def reject(self, sid: str, patch_sha256: str) -> SessionSnapshot:
        """Reject the pending patch. One atomic database change, so no run lock is needed (a worker that is
        still returning from the run that reached WAITING_APPROVAL must not make a rejection fail)."""
        row = self._row(sid)
        if row.state is not State.WAITING_APPROVAL:
            raise OrchestratorError(
                "invalid_state",
                f"Session is {row.state.value}, not waiting for approval.",
            )
        try:
            self._store.reject_patch(sid, patch_sha256)
        except KeyError as exc:
            raise OrchestratorError(
                "patch_mismatch", "No pending patch matches this hash."
            ) from exc
        except StaleStateError as exc:
            raise OrchestratorError(
                "invalid_state", "The session changed; nothing was rejected."
            ) from exc
        self._event(
            sid,
            State.REJECTED,
            "state",
            "Patch rejected by user. No changes were applied.",
        )
        self._finalize(sid)
        return self.get(sid)

    def cancel(
        self, sid: str, message: str = "Session cancelled by user."
    ) -> SessionSnapshot:
        row = self._row(sid)
        if row.state in TERMINAL_STATES:
            raise OrchestratorError(
                "invalid_state", f"Session already finished ({row.state.value})."
            )
        try:
            self._transition(sid, State.CANCELLED, message=message)
        except StaleStateError as exc:
            raise OrchestratorError(
                "invalid_state", "The session changed; try again."
            ) from exc
        with self._guard:
            busy = sid in self._running
        if not busy:  # a running loop notices the new state and cleans up itself
            self._finalize(sid)
        return self.get(sid)

    # ---- housekeeping for a long-running server ----
    def expire_stale_waiting(self, older_than_seconds: float) -> list[str]:
        """Cancel sessions that have waited for approval too long (they hold a container)."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        ).isoformat()
        expired = []
        for sid in self._store.waiting_older_than(cutoff):
            try:
                self.cancel(sid, "Session expired while waiting for approval.")
                expired.append(sid)
            except OrchestratorError:
                pass  # approved, rejected or cancelled in the meantime
        return expired

    def recover_interrupted(self) -> list[str]:
        """After a restart: sessions that were mid-run cannot continue. WAITING_APPROVAL ones can."""
        interrupted = [
            s
            for s in State
            if s not in TERMINAL_STATES and s is not State.WAITING_APPROVAL
        ]
        recovered = []
        for sid in self._store.sessions_in_states(interrupted):
            with self._guard:
                if sid in self._running:
                    continue
            self.fail_session(
                sid,
                "server_restarted",
                "The server restarted while this session was running.",
            )
            recovered.append(sid)
        return recovered

    def fail_session(self, sid: str, code: str, message: str) -> None:
        self._fail(sid, code, message)
        if self._row(sid).state in TERMINAL_STATES:
            self._finalize(sid)

    def shutdown(self) -> None:
        """Destroy every live sandbox. Session state stays in the database; approval re-creates the sandbox."""
        for sid in list(self._live):
            live = self._live.pop(sid, None)
            if live is not None:
                self._destroy(live.sandbox)

    def get(self, sid: str) -> SessionSnapshot:
        row = self._row(sid)
        pending = None
        if row.state is State.WAITING_APPROVAL:
            patch = self._store.latest_patch(sid, "proposed")
            pending = self._view(patch) if patch else None
        report = (
            DebugReport.model_validate_json(row.report_json)
            if row.report_json
            else None
        )
        if report is None and row.state in TERMINAL_STATES:
            # The state flips before cleanup finishes; never expose a finished session without its report.
            report = self._build_report(row)
            self._store.save_report(sid, report.model_dump_json())
        return SessionSnapshot(
            session_id=sid,
            state=row.state,
            ui_state=ui_state(row.state, row.error_code),
            message=row.message,
            error_code=row.error_code,
            patch_attempts=row.patch_attempts,
            pending_patch=pending,
            report=report,
        )

    def events(self, sid: str) -> list[AgentEvent]:
        self._row(sid)
        return self._store.steps(sid)

    # ================================================================ run loop
    @contextmanager
    def _running_guard(self, sid: str) -> Iterator[None]:
        with self._guard:
            if sid in self._running:
                raise OrchestratorError(
                    "session_busy", "This session is already being processed."
                )
            self._running.add(sid)
        try:
            yield
        finally:
            with self._guard:
                self._running.discard(sid)

    def _row(self, sid: str) -> SessionRow:
        try:
            return self._store.get_session(sid)
        except KeyError as exc:
            raise OrchestratorError(
                "session_not_found", f"No session {sid!r}."
            ) from exc

    def _run(self, sid: str) -> None:
        segment_start = self._clock()
        try:
            self._advance(sid, segment_start)
        except Exception:  # noqa: BLE001 - last line of defence; never leave a session half-running
            logger.exception("orchestrator_internal_error session=%s", sid)
            self._fail(
                sid,
                "internal_error",
                "Unexpected internal error. The session was stopped.",
            )
        finally:
            self._store.add_active_seconds(sid, self._clock() - segment_start)
            self._flush(sid)
            if self._row(sid).state in TERMINAL_STATES:
                self._finalize(sid)

    def _advance(self, sid: str, segment_start: float) -> None:
        handlers = {
            State.INITIALIZE: self._do_initialize,
            State.ANALYZE_REPOSITORY: self._do_analyze_repository,
            State.RUN_TESTS: self._do_run_tests,
            State.ANALYZE_FAILURE: self._do_analyze_failure,
            State.INVESTIGATE: self._do_investigate,
            State.FORM_HYPOTHESIS: self._do_form_hypothesis,
            State.GENERATE_PATCH: self._do_generate_patch,
            State.APPLY_PATCH: self._do_apply_patch,
            State.VERIFY: self._do_verify,
            State.RETRY: self._do_retry,
        }
        while True:
            row = self._row(sid)
            if row.state in TERMINAL_STATES or row.state is State.WAITING_APPROVAL:
                return
            try:
                if (
                    row.active_seconds + (self._clock() - segment_start)
                    > self._config.max_active_seconds
                ):
                    raise _Abort(
                        "session_timeout", "Session exceeded the allowed time."
                    )
                handlers[row.state](sid, row, self._live_for(sid, row))
            except _Abort as abort:
                self._fail(sid, abort.code, str(abort))
            except StaleStateError:
                continue  # someone (e.g. cancel) changed the state; re-read it
            finally:
                self._flush(sid)

    # ---------------------------------------------------------- sandbox lifecycle
    def _live_for(self, sid: str, row: SessionRow) -> _Live:
        live = self._live.get(sid)
        if live is not None:
            return live
        try:
            sandbox = self._factory()
        except SandboxError as exc:  # e.g. Docker is not running
            raise _Abort(
                next(code for cls, code in _SANDBOX_CODES if isinstance(exc, cls)),
                str(exc),
            ) from exc
        try:
            sandbox.start()
            toolbox = Toolbox(sandbox, self._tool_limits)
            loaded = toolbox.load_repository({"path": row.repo_path})
            if not loaded.ok:
                raise _Abort(
                    loaded.error_code or "load_failed",
                    loaded.error or "Could not load the repository.",
                )
        except SandboxError as exc:
            self._destroy(sandbox)
            raise _Abort(
                next(code for cls, code in _SANDBOX_CODES if isinstance(exc, cls)),
                str(exc),
            ) from exc
        except _Abort:
            self._destroy(sandbox)
            raise
        live = _Live(
            sandbox, toolbox, AgentTools(toolbox)
        )  # flushed=0: load_repository is logged too
        self._live[sid] = live
        return live

    @staticmethod
    def _destroy(sandbox: SandboxHandle) -> None:
        try:
            sandbox.destroy()
        except Exception:  # noqa: BLE001
            logger.exception("sandbox_destroy_failed")

    def _finalize(self, sid: str) -> None:
        """Terminal state reached: write the report once, then destroy the sandbox."""
        row = self._row(sid)
        if row.report_json is None:
            self._store.save_report(sid, self._build_report(row).model_dump_json())
        live = self._live.pop(sid, None)
        if live is not None:
            self._destroy(live.sandbox)

    # ---------------------------------------------------------- transitions / events
    def _transition(
        self,
        sid: str,
        new: State,
        *,
        message: str = "",
        error_code: str | None = None,
        verification: VerificationResult | None = None,
    ) -> None:
        current = self._row(sid).state
        assert_transition(current, new)
        if new is State.SUCCESS and (
            verification is None
            or not verification.authentic()
            or not verification.verified
        ):
            raise InvalidTransition(
                "SUCCESS requires a sealed, verified VerificationResult from the Verifier"
            )
        self._store.set_state(
            sid, expected=current, new=new, message=message, error_code=error_code
        )
        self._event(
            sid, new, "error" if new is State.FAILED else "state", message or new.value
        )

    def _fail(self, sid: str, code: str, message: str) -> None:
        try:
            self._transition(sid, State.FAILED, message=message, error_code=code)
        except (StaleStateError, InvalidTransition):
            pass  # already terminal (e.g. cancelled)

    def _event(self, sid: str, state: State, kind: str, message: str) -> None:
        event = self._store.add_step(sid, state, kind, message)
        logger.info(
            "agent_event session=%s state=%s kind=%s message=%s",
            sid,
            state.value,
            kind,
            message,
        )
        if self._on_event:
            self._on_event(event)

    def _flush(self, sid: str) -> None:
        live = self._live.get(sid)
        if live is not None and len(live.toolbox.calls) > live.flushed:
            self._store.add_tool_calls(sid, live.toolbox.calls[live.flushed :])
            live.flushed = len(live.toolbox.calls)

    # ================================================================ state handlers
    def _do_initialize(self, sid: str, row: SessionRow, live: _Live) -> None:
        self._transition(
            sid,
            State.ANALYZE_REPOSITORY,
            message="Repository copied into an isolated sandbox.",
        )

    def _do_analyze_repository(self, sid: str, row: SessionRow, live: _Live) -> None:
        listing = live.toolbox.list_files(
            {"max_files": 1000}, count_against_budget=False
        )
        if not listing.ok:
            raise _Abort(
                listing.error_code or "list_failed",
                listing.error or "Could not list files.",
            )
        live.files = listing.files
        if not any(f.endswith(".py") for f in listing.files):
            raise _Abort(
                "invalid_project",
                "This does not look like a Python project: no .py files found.",
            )
        self._transition(
            sid,
            State.RUN_TESTS,
            message=f"Analyzed repository ({len(listing.files)} files).",
        )

    def _do_run_tests(self, sid: str, row: SessionRow, live: _Live) -> None:
        result = live.toolbox.run_tests(
            {"timeout_seconds": self._config.test_timeout_seconds},
            count_against_budget=False,
        )
        if not result.ok:
            raise _Abort(
                result.error_code or "test_run_failed",
                result.error or "The tests could not be run.",
            )
        self._store.save_reproduction(sid, result.model_dump_json())
        if result.status == "failed":
            self._transition(
                sid,
                State.ANALYZE_FAILURE,
                message=f"Reproduced failure: {result.summary}.",
            )
        elif result.status == "passed":
            self._transition(
                sid,
                State.NO_FAILURE,
                message="All tests pass. There is no failure to debug.",
            )
        elif result.status == "timeout":
            raise _Abort("test_timeout", TEST_TIMEOUT)
        else:
            raise _Abort(
                "tests_unusable",
                f"The tests could not be run or interpreted: {result.summary}",
            )

    def _reproduction(self, row: SessionRow) -> RunTestsOutput:
        assert row.reproduction_json is not None
        return RunTestsOutput.model_validate_json(row.reproduction_json)

    def _do_analyze_failure(self, sid: str, row: SessionRow, live: _Live) -> None:
        failing = self._reproduction(row).failed_tests
        label = failing[0].test_id if failing else "unknown test"
        self._transition(
            sid, State.INVESTIGATE, message=f"Located failing test: {label}"
        )

    def _do_investigate(self, sid: str, row: SessionRow, live: _Live) -> None:
        context = InvestigationContext(
            session_id=sid, failure=self._reproduction(row), files=live.files[:200]
        )
        try:
            hypothesis = self._engine.investigate(context, live.tools)
            hypothesis = Hypothesis.model_validate(
                hypothesis.model_dump()
                if isinstance(hypothesis, Hypothesis)
                else hypothesis
            )
        except ModelUnavailableError as exc:
            raise _Abort("ai_unavailable", AI_UNAVAILABLE) from exc
        except InvestigationFailed as exc:
            raise _Abort(
                "investigation_failed", f"I could not identify the root cause. {exc}"
            ) from exc
        except (ValidationError, TypeError, AttributeError) as exc:
            raise _Abort(
                "agent_error", "The agent returned an unusable hypothesis."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("engine_error session=%s", sid)
            raise _Abort(
                "agent_error", "The reasoning engine failed unexpectedly."
            ) from exc
        self._store.save_hypothesis(sid, hypothesis.model_dump_json())
        used = self._tool_limits.max_tool_calls - live.toolbox.budget_remaining
        self._transition(
            sid,
            State.FORM_HYPOTHESIS,
            message=f"Inspected the codebase ({used} tool calls).",
        )

    def _hypothesis(self, row: SessionRow) -> Hypothesis:
        assert row.hypothesis_json is not None
        return Hypothesis.model_validate_json(row.hypothesis_json)

    def _do_form_hypothesis(self, sid: str, row: SessionRow, live: _Live) -> None:
        hypothesis = self._hypothesis(row)
        try:
            path = validate_repo_path(hypothesis.file)
        except Exception as exc:  # noqa: BLE001
            raise _Abort(
                "invalid_hypothesis", "The root cause points at an invalid path."
            ) from exc
        if path not in live.files:
            raise _Abort(
                "invalid_hypothesis",
                "The root cause points at a file that is not in the repository.",
            )
        where = f"{path}:{hypothesis.line}" if hypothesis.line else path
        self._transition(
            sid, State.GENERATE_PATCH, message=f"Identified root cause in {where}."
        )

    def _do_generate_patch(self, sid: str, row: SessionRow, live: _Live) -> None:
        hypothesis = self._hypothesis(row)
        failure = self._reproduction(row)
        feedback = list(row.feedback)
        attempts = row.patch_attempts
        while True:
            if attempts >= self._config.max_patch_attempts:
                raise _Abort("attempts_exhausted", GAVE_UP)
            attempts = self._store.increment_attempts(sid)
            context = PatchContext(
                session_id=sid,
                attempt=attempts,
                max_attempts=self._config.max_patch_attempts,
                hypothesis=hypothesis,
                failure=failure,
                previous_attempts=feedback,
            )
            try:
                proposal = self._engine.propose_patch(context, live.tools)
                diff = proposal.diff
            except ModelUnavailableError as exc:
                raise _Abort("ai_unavailable", AI_UNAVAILABLE) from exc
            except PatchGenerationError as exc:
                feedback.append(
                    self._reject_attempt(sid, attempts, None, "no_patch", str(exc))
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("engine_error session=%s", sid)
                raise _Abort(
                    "agent_error", "The reasoning engine failed unexpectedly."
                ) from exc
            checked = live.toolbox.check_patch({"diff": diff})
            if not checked.ok:
                feedback.append(
                    self._reject_attempt(
                        sid,
                        attempts,
                        None,
                        checked.error_code or "invalid_patch",
                        checked.error or "The patch was rejected.",
                    )
                )
                continue
            if self._store.has_patch(sid, checked.sha256):
                feedback.append(
                    self._reject_attempt(
                        sid,
                        attempts,
                        checked.sha256,
                        "duplicate_patch",
                        "This exact patch was already tried.",
                    )
                )
                continue
            self._store.add_patch(
                sid,
                attempt=attempts,
                sha256=checked.sha256,
                diff=checked.diff,
                summary=proposal.summary,
                files=checked.files,
            )
            self._transition(
                sid,
                State.WAITING_APPROVAL,
                message=f"Patch proposed ({len(checked.files)} file, +{checked.insertions}/-{checked.deletions}). "
                "Waiting for your approval.",
            )
            return

    def _reject_attempt(
        self, sid: str, attempt: int, sha: str | None, code: str, message: str
    ) -> AttemptFeedback:
        feedback = AttemptFeedback(
            attempt=attempt, patch_sha256=sha, reason_code=code, message=message[:500]
        )
        self._store.append_feedback(sid, feedback)
        self._event(sid, State.GENERATE_PATCH, "info", f"{PATCH_UNSAFE} ({code})")
        return feedback

    def _do_apply_patch(self, sid: str, row: SessionRow, live: _Live) -> None:
        patch = self._store.latest_patch(sid, "approved")
        if patch is None:
            raise _Abort("approval_missing", "No approved patch was found.")
        live.toolbox.register_approval(
            patch.sha256
        )  # only reached after a persisted approval
        applied = live.toolbox.apply_patch({"diff": patch.diff})
        if not applied.ok:
            self._store.set_patch_status(
                sid, patch.sha256, expected="approved", new="apply_failed"
            )
            raise _Abort("patch_apply_failed", f"{PATCH_UNSAFE} ({applied.error_code})")
        shown = live.toolbox.git_diff(count_against_budget=False)
        if not shown.ok or sorted(shown.files_changed) != sorted(patch.files):
            raise _Abort(
                "unexpected_changes",
                "Applying the patch changed files other than the approved ones.",
            )
        self._store.set_patch_status(
            sid, patch.sha256, expected="approved", new="applied"
        )
        self._transition(
            sid,
            State.VERIFY,
            message=f"Applied approved patch to {', '.join(patch.files)}.",
        )

    def _do_verify(self, sid: str, row: SessionRow, live: _Live) -> None:
        patch = self._store.latest_patch(sid, "applied")
        if patch is None:
            raise _Abort("approval_missing", "No applied patch was found.")
        after = live.toolbox.run_tests(
            {"timeout_seconds": self._config.test_timeout_seconds},
            count_against_budget=False,
        )
        result = Verifier.verify(
            before=self._reproduction(row),
            after=after,
            patch_sha256=patch.sha256,
            files_changed=patch.files,
        )
        self._store.save_verification(
            sid, row.patch_attempts, result.verified, result.model_dump_json()
        )
        if result.verified:
            self._store.set_patch_status(
                sid, patch.sha256, expected="applied", new="verified"
            )
            b, a = result.before, result.after
            self._transition(
                sid,
                State.SUCCESS,
                verification=result,
                message=f"Verification passed: {a.failed} failed, {a.passed} passed "
                f"(before: {b.failed} failed, {b.passed} passed).",
            )
            return
        self._store.set_patch_status(
            sid, patch.sha256, expected="applied", new="failed_verification"
        )
        self._store.append_feedback(
            sid,
            AttemptFeedback(
                attempt=row.patch_attempts,
                patch_sha256=patch.sha256,
                reason_code=result.reason_code,
                message=f"{result.message} Test run: {after.summary}"[:500],
            ),
        )
        self._event(
            sid, State.VERIFY, "info", f"{NOT_VERIFIED_MESSAGE} ({result.reason_code})"
        )
        if row.patch_attempts < self._config.max_patch_attempts:
            self._transition(
                sid,
                State.RETRY,
                message="Verification failed; preparing another attempt.",
            )
        else:
            raise _Abort("attempts_exhausted", GAVE_UP)

    def _do_retry(self, sid: str, row: SessionRow, live: _Live) -> None:
        reset = live.toolbox.reset_changes()
        if not reset.ok or not reset.clean:
            raise _Abort(
                "reset_failed", "Could not restore the original repository state."
            )
        self._transition(
            sid, State.GENERATE_PATCH, message="Restored the original repository state."
        )

    # ================================================================ reporting
    @staticmethod
    def _view(patch: PatchRow) -> PatchView:
        return PatchView(
            sha256=patch.sha256,
            diff=patch.diff,
            summary=patch.summary,
            files=patch.files,
            attempt=patch.attempt,
            status=patch.status,
        )

    def _build_report(self, row: SessionRow) -> DebugReport:
        before = (
            RunTestsOutput.model_validate_json(row.reproduction_json)
            if row.reproduction_json
            else None
        )
        hypothesis = (
            Hypothesis.model_validate_json(row.hypothesis_json)
            if row.hypothesis_json
            else None
        )
        raw = self._store.latest_verification_json(row.id)
        verification = VerificationResult.model_validate_json(raw) if raw else None
        # Only a verified result may carry a verified patch into the report; otherwise show the latest proposal.
        patch = (
            self._store.latest_patch(row.id, "verified")
            if row.state is State.SUCCESS
            else None
        ) or self._store.latest_patch(row.id)
        success = (
            row.state is State.SUCCESS
            and verification is not None
            and verification.verified
        )
        return DebugReport(
            session_id=row.id,
            outcome=row.state,
            message=row.message,
            headline="DEBUGGING COMPLETE: fix verified by re-running the tests."
            if success
            else row.message,
            root_cause=RootCauseReport(
                file=hypothesis.file,
                line=hypothesis.line,
                description=hypothesis.description,
            )
            if hypothesis
            else None,
            before=before.counts if before else None,
            after=verification.after if verification else None,
            failing_tests_before=[f.test_id for f in before.failed_tests]
            if before
            else [],
            failing_tests_after=(verification.still_failing + verification.new_failures)
            if verification
            else [],
            files_changed=patch.files if patch else [],
            patch_diff=patch.diff if patch else None,
            patch_sha256=patch.sha256 if patch else None,
            verification=verification,
            patch_attempts=row.patch_attempts,
            attempt_feedback=row.feedback,
        )
