"""Structured data exchanged between the orchestrator, the reasoning engine and the API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repodoctor.tools.models import Counts, RunTestsOutput

from .states import State
from .verifier import VerificationResult


@dataclass(frozen=True)
class AgentConfig:
    max_patch_attempts: int = 3  # every patch the engine is asked for counts, valid or not
    max_active_seconds: int = 900  # working time; time spent waiting for a human is excluded
    test_timeout_seconds: int | None = None  # None = sandbox default


# ---- what the reasoning engine receives and returns ----
class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    line: int | None = Field(default=None, ge=1)
    description: str = Field(min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"


class PatchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    diff: str
    summary: str = Field(default="", max_length=1000)


class AttemptFeedback(BaseModel):
    """Why an earlier attempt did not work; passed back to the engine on retry."""
    attempt: int
    patch_sha256: str | None = None
    reason_code: str
    message: str


class InvestigationContext(BaseModel):
    session_id: str
    failure: RunTestsOutput
    files: list[str]


class PatchContext(BaseModel):
    session_id: str
    attempt: int
    max_attempts: int
    hypothesis: Hypothesis
    failure: RunTestsOutput
    previous_attempts: list[AttemptFeedback] = Field(default_factory=list)


# ---- what callers (API/UI) see ----
class RootCauseReport(BaseModel):
    file: str
    line: int | None = None
    description: str


class PatchView(BaseModel):
    sha256: str
    diff: str
    summary: str
    files: list[str]
    attempt: int
    status: str


class DebugReport(BaseModel):
    session_id: str
    outcome: State
    headline: str
    message: str
    root_cause: RootCauseReport | None = None
    before: Counts | None = None
    after: Counts | None = None
    failing_tests_before: list[str] = Field(default_factory=list)
    failing_tests_after: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    patch_diff: str | None = None
    patch_sha256: str | None = None
    verification: VerificationResult | None = None
    patch_attempts: int = 0
    attempt_feedback: list[AttemptFeedback] = Field(default_factory=list)


class SessionSnapshot(BaseModel):
    session_id: str
    state: State
    ui_state: str
    message: str
    error_code: str | None = None
    patch_attempts: int = 0
    pending_patch: PatchView | None = None
    report: DebugReport | None = None


class AgentEvent(BaseModel):
    seq: int
    session_id: str
    state: State
    kind: str  # "state" (a step), "info" (neutral note), "warning" (something went wrong but the run continues), "error" (the run failed)
    message: str  # high-level progress only; never model reasoning
    created_at: str
