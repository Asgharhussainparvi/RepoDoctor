"""Parse pytest output into structured results. Pure functions, no I/O.

We read pytest's final summary line and its short-test-summary block. Both are
printed last, so they survive the sandbox's head+tail output truncation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

RunStatus = Literal["passed", "failed", "error", "timeout", "no_tests", "unparseable"]

_PART = (
    r"\d+ (?:failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?|rerun)"
)
_BODY_RE = re.compile(rf"^{_PART}(?:, {_PART})*$")
_SUMMARY_RE = re.compile(
    r"^(?P<body>.+?) in (?P<secs>\d+(?:\.\d+)?)s(?: \(\d+:\d{2}:\d{2}\))?$"
)
_COUNT_RE = re.compile(
    r"(\d+) (failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?|rerun)"
)
_FAILED_RE = re.compile(r"^(?P<kind>FAILED|ERROR) (?P<id>\S.*?)(?: - (?P<msg>.*))?$")
_KEY = {
    "warning": "warnings",
    "warnings": "warnings",
    "error": "errors",
    "errors": "errors",
}


@dataclass
class ParsedCounts:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    deselected: int = 0
    warnings: int = 0


@dataclass
class ParsedFailure:
    test_id: str
    kind: str  # "FAILED" or "ERROR"
    message: str = ""


@dataclass
class ParsedRun:
    counts: ParsedCounts = field(default_factory=ParsedCounts)
    duration_seconds: float | None = None
    failures: list[ParsedFailure] = field(default_factory=list)


def parse_pytest_output(output: str) -> ParsedRun | None:
    """Return the parsed run, or None if no trustworthy summary line was found."""
    lines = output.splitlines()
    summary_idx = None
    counts = ParsedCounts()
    duration = None
    for idx in range(len(lines) - 1, -1, -1):  # the LAST matching line wins
        core = lines[idx].strip().strip("=").strip()
        match = _SUMMARY_RE.match(core)
        if not match:
            continue
        body = match.group("body")
        if body != "no tests ran" and not _BODY_RE.match(body):
            continue
        summary_idx = idx
        duration = float(match.group("secs"))
        for number, word in _COUNT_RE.findall(body):
            key = _KEY.get(word, word)
            if hasattr(counts, key):
                setattr(counts, key, int(number))
        break
    if summary_idx is None:
        return None
    return ParsedRun(
        counts=counts,
        duration_seconds=duration,
        failures=_parse_failures(lines, summary_idx),
    )


def _parse_failures(lines: list[str], summary_idx: int) -> list[ParsedFailure]:
    start = None
    for idx in range(summary_idx - 1, -1, -1):
        if "short test summary info" in lines[idx]:
            start = idx + 1
            break
    if start is None:
        return []
    failures: list[ParsedFailure] = []
    seen: set[str] = set()
    for line in lines[start:summary_idx]:
        match = _FAILED_RE.match(line)
        if not match or match.group("id") in seen:
            continue
        seen.add(match.group("id"))
        failures.append(
            ParsedFailure(
                match.group("id"), match.group("kind"), (match.group("msg") or "")[:300]
            )
        )
    return failures


def classify_run(
    exit_code: int | None, parsed: ParsedRun | None, timed_out: bool
) -> RunStatus:
    """Decide the run status. A pass requires exit code AND counts to agree.

    Anything inconsistent is "unparseable", which is never treated as a pass.
    """
    if timed_out:
        return "timeout"
    if parsed is None or exit_code is None:
        return "unparseable"
    c = parsed.counts
    if exit_code == 0:
        if c.failed == 0 and c.errors == 0 and c.passed > 0:
            return "passed"
        if c.failed == 0 and c.errors == 0:
            return "no_tests"  # e.g. everything skipped: nothing was verified
        return "unparseable"
    if exit_code == 1:
        return "failed" if (c.failed > 0 or c.errors > 0) else "unparseable"
    if exit_code == 5:
        return "no_tests"
    return "error"  # 2 interrupted/collection error, 3 internal error, 4 usage error, others
