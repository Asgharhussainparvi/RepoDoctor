"""Typed inputs and outputs for every tool (Pydantic v2).

Inputs forbid unknown fields, so a model that invents parameters gets a clear error.
Every output carries ok/error_code/error. Fields have safe defaults, so a failed call
still returns a well-formed object (and `status` defaults are never a pass).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .pytest_parser import RunStatus


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolOutput(BaseModel):
    ok: bool = True
    error_code: str | None = None
    error: str | None = None


# ---- list_files ----
class ListFilesInput(ToolInput):
    path: str = "."
    max_files: int = Field(default=200, ge=1, le=1000)


class ListFilesOutput(ToolOutput):
    path: str = "."
    files: list[str] = Field(default_factory=list)
    truncated: bool = False


# ---- read_file ----
class ReadFileInput(ToolInput):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> "ReadFileInput":
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class ReadFileOutput(ToolOutput):
    path: str = ""
    start_line: int = 1
    end_line: int = 0
    total_lines: int = 0
    content: str = ""
    has_more: bool = False
    long_lines_cut: bool = False

    def numbered(self) -> str:
        """Render with line numbers for prompts: '  12 | code'."""
        rows = self.content.split("\n") if self.content else []
        return "\n".join(f"{self.start_line + k:>5} | {row}" for k, row in enumerate(rows))


# ---- search_code ----
class SearchCodeInput(ToolInput):
    pattern: str = Field(min_length=1, max_length=200)
    path: str = "."
    literal: bool = True
    case_sensitive: bool = False
    max_results: int = Field(default=50, ge=1, le=200)

    @field_validator("pattern")
    @classmethod
    def _plain_text(cls, value: str) -> str:
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("pattern must be a single line without NUL bytes")
        return value


class SearchMatch(BaseModel):
    path: str
    line: int
    text: str


class SearchCodeOutput(ToolOutput):
    matches: list[SearchMatch] = Field(default_factory=list)
    truncated: bool = False


# ---- run_tests ----
class RunTestsInput(ToolInput):
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)


class Counts(BaseModel):
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    deselected: int = 0
    warnings: int = 0


class FailedTest(BaseModel):
    test_id: str
    kind: str = "FAILED"
    message: str = ""


class RunTestsOutput(ToolOutput):
    status: RunStatus = "error"
    exit_code: int | None = None
    timed_out: bool = False
    counts: Counts = Field(default_factory=Counts)
    failed_tests: list[FailedTest] = Field(default_factory=list)
    duration_seconds: float = 0.0
    summary: str = ""
    output_excerpt: str = ""
    output_truncated: bool = False


# ---- git_diff / patches ----
class GitDiffInput(ToolInput):
    pass


class GitDiffOutput(ToolOutput):
    has_changes: bool = False
    diff: str = ""
    files_changed: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0


class CheckPatchInput(ToolInput):
    diff: str


class CheckPatchOutput(ToolOutput):
    sha256: str = ""
    diff: str = ""  # normalised text; this exact string is what sha256 covers
    files: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0


class ApplyPatchInput(ToolInput):
    diff: str


class ApplyPatchOutput(ToolOutput):
    sha256: str = ""
    files_changed: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0


# ---- reset_changes / load_repository ----
class ResetChangesInput(ToolInput):
    pass


class ResetChangesOutput(ToolOutput):
    clean: bool = False


class LoadRepositoryInput(ToolInput):
    path: str


class LoadRepositoryOutput(ToolOutput):
    file_count: int = 0
    total_bytes: int = 0
    baseline_commit: str = ""
