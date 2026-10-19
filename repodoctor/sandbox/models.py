"""Typed results returned by the sandbox."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecResult:
    argv: tuple[str, ...]
    exit_code: int | None  # None when the sandbox itself had to be killed
    output: str  # stdout + stderr merged, capped (head + tail)
    timed_out: bool
    truncated: bool
    duration_seconds: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class UploadSummary:
    file_count: int
    total_bytes: int
    skipped_links: int  # symlinks and special files are never copied
