"""Toolbox: the seven RepoDoctor tools over an execution backend.

Every public tool method:
  * validates its input (typed Pydantic model, unknown fields rejected),
  * runs only through the backend (the Docker SandboxManager in production),
  * has a timeout, returns a structured result and NEVER raises,
  * is logged and recorded in `toolbox.calls`.

Agent-facing tools count against a per-session call budget. Orchestrator-only
operations (load_repository, check_patch, apply_patch, reset_changes) do not, and
apply_patch additionally requires a registered approval for the exact patch hash.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, TypeVar

from pydantic import BaseModel, ValidationError

from repodoctor.sandbox.errors import (
    DockerUnavailableError,
    SandboxError,
    SandboxImageMissingError,
    SandboxNotRunningError,
    UploadRejectedError,
)
from repodoctor.sandbox.models import ExecResult, UploadSummary

from .limits import ToolLimits
from .models import (
    ApplyPatchInput, ApplyPatchOutput, CheckPatchInput, CheckPatchOutput, Counts, FailedTest,
    GitDiffInput, GitDiffOutput, ListFilesInput, ListFilesOutput, LoadRepositoryInput,
    LoadRepositoryOutput, ReadFileInput, ReadFileOutput, ResetChangesInput, ResetChangesOutput,
    RunTestsInput, RunTestsOutput, SearchCodeInput, SearchCodeOutput, SearchMatch, ToolOutput,
)
from .paths import InvalidPathError, validate_repo_path
from .patch import PatchRejected, validate_patch
from .pytest_parser import classify_run, parse_pytest_output

logger = logging.getLogger("repodoctor.tools")

PYTEST_ARGV = [
    "python", "-m", "pytest", "-q", "-rfE", "--tb=short", "-p", "no:cacheprovider",
    "-o", "addopts=",  # a repo's own addopts (e.g. -qq) can hide the summary line we rely on
]
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# Runs inside the sandbox (python - <args>, script on stdin). Prints ONE JSON line.
READ_SCRIPT = r"""
import json, os, sys
root, path = os.path.realpath(sys.argv[1]), sys.argv[2]
start, end, max_bytes, max_line = (int(a) for a in sys.argv[3:7])
def out(**kw):
    print(json.dumps(kw))
    sys.exit(0)
real = os.path.realpath(os.path.join(root, path))
if real != root and not real.startswith(root + os.sep):
    out(error="outside_workspace")
if not os.path.isfile(real):
    out(error="not_found")
size = os.path.getsize(real)
if size > max_bytes:
    out(error="file_too_large", size=size)
with open(real, "rb") as fh:
    data = fh.read()
if b"\x00" in data[:8192]:
    out(error="binary_file")
lines = data.decode("utf-8", errors="replace").split("\n")
if lines and lines[-1] == "":
    lines.pop()
chosen = lines[start - 1:end]
cut = any(len(l) > max_line for l in chosen)
out(total_lines=len(lines), lines=[l[:max_line] for l in chosen], long_lines_cut=cut)
"""

T = TypeVar("T", bound=ToolOutput)


class ExecutionBackend(Protocol):
    """What the toolbox needs. SandboxManager satisfies this."""

    @property
    def workspace(self) -> str: ...
    def upload_repo(self, source: str | Path) -> UploadSummary: ...
    def exec(self, argv: Sequence[str], timeout: int | None = None, stdin: bytes | None = None) -> ExecResult: ...


class ToolFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolCallRecord:
    tool: str
    ok: bool
    error_code: str | None
    duration_seconds: float
    budgeted: bool


_SANDBOX_CODES: tuple[tuple[type[Exception], str], ...] = (
    (DockerUnavailableError, "sandbox_unavailable"),
    (SandboxImageMissingError, "sandbox_image_missing"),
    (SandboxNotRunningError, "sandbox_not_running"),
    (UploadRejectedError, "upload_rejected"),
    (SandboxError, "sandbox_error"),
)


class Toolbox:
    def __init__(self, backend: ExecutionBackend, limits: ToolLimits | None = None) -> None:
        self._backend = backend
        self.limits = limits or ToolLimits()
        self._baseline: str | None = None
        self._budgeted_calls = 0
        self._approved: set[str] = set()
        self.calls: list[ToolCallRecord] = []

    # ------------------------------------------------------------------ state
    @property
    def baseline_commit(self) -> str | None:
        return self._baseline

    @property
    def budget_remaining(self) -> int:
        return max(0, self.limits.max_tool_calls - self._budgeted_calls)

    def register_approval(self, patch_sha256: str) -> None:
        """Called ONLY by the orchestrator after a persisted human approval.

        Never expose this (or apply_patch) to the model.
        """
        if not isinstance(patch_sha256, str) or not _SHA_RE.match(patch_sha256):
            raise ValueError("patch_sha256 must be 64 lowercase hex characters")
        self._approved.add(patch_sha256)

    # ------------------------------------------------------- orchestrator ops
    def load_repository(self, inp: LoadRepositoryInput | Mapping[str, Any] | None = None) -> LoadRepositoryOutput:
        return self._invoke("load_repository", LoadRepositoryInput, LoadRepositoryOutput, inp,
                            self._load_repository, budgeted=False, needs_repo=False)

    def check_patch(self, inp: CheckPatchInput | Mapping[str, Any] | None = None) -> CheckPatchOutput:
        return self._invoke("check_patch", CheckPatchInput, CheckPatchOutput, inp,
                            self._check_patch, budgeted=False)

    def apply_patch(self, inp: ApplyPatchInput | Mapping[str, Any] | None = None) -> ApplyPatchOutput:
        return self._invoke("apply_patch", ApplyPatchInput, ApplyPatchOutput, inp,
                            self._apply_patch, budgeted=False)

    def reset_changes(self, inp: ResetChangesInput | Mapping[str, Any] | None = None) -> ResetChangesOutput:
        return self._invoke("reset_changes", ResetChangesInput, ResetChangesOutput, inp,
                            self._reset_changes, budgeted=False)

    # -------------------------------------------------------- agent-facing tools
    def list_files(self, inp: ListFilesInput | Mapping[str, Any] | None = None, *,
               count_against_budget: bool = True) -> ListFilesOutput:
        return self._invoke("list_files", ListFilesInput, ListFilesOutput, inp, self._list_files, budgeted=count_against_budget)

    def read_file(self, inp: ReadFileInput | Mapping[str, Any] | None = None, *,
               count_against_budget: bool = True) -> ReadFileOutput:
        return self._invoke("read_file", ReadFileInput, ReadFileOutput, inp, self._read_file, budgeted=count_against_budget)

    def search_code(self, inp: SearchCodeInput | Mapping[str, Any] | None = None, *,
               count_against_budget: bool = True) -> SearchCodeOutput:
        return self._invoke("search_code", SearchCodeInput, SearchCodeOutput, inp, self._search_code, budgeted=count_against_budget)

    def run_tests(self, inp: RunTestsInput | Mapping[str, Any] | None = None, *,
               count_against_budget: bool = True) -> RunTestsOutput:
        return self._invoke("run_tests", RunTestsInput, RunTestsOutput, inp, self._run_tests, budgeted=count_against_budget)

    def git_diff(self, inp: GitDiffInput | Mapping[str, Any] | None = None, *,
               count_against_budget: bool = True) -> GitDiffOutput:
        return self._invoke("git_diff", GitDiffInput, GitDiffOutput, inp, self._git_diff, budgeted=count_against_budget)

    # ----------------------------------------------------------- plumbing
    def _invoke(
        self, name: str, in_model: type[BaseModel], out_model: type[T], raw: Any,
        handler: Callable[[Any], T], *, budgeted: bool = True, needs_repo: bool = True,
    ) -> T:
        started = time.monotonic()
        try:
            if budgeted and self._budgeted_calls >= self.limits.max_tool_calls:
                raise ToolFailure(
                    "tool_budget_exceeded", f"Tool call budget of {self.limits.max_tool_calls} is exhausted."
                )
            if budgeted:
                self._budgeted_calls += 1
            inp = self._coerce(in_model, raw)
            if needs_repo and self._baseline is None:
                raise ToolFailure("repository_not_loaded", "No repository has been loaded into the sandbox.")
            result = handler(inp)
        except ToolFailure as exc:
            result = out_model(ok=False, error_code=exc.code, error=str(exc))
        except SandboxError as exc:
            code = next(c for cls, c in _SANDBOX_CODES if isinstance(exc, cls))
            result = out_model(ok=False, error_code=code, error=str(exc))
        except Exception:  # noqa: BLE001 - tools must never raise into the agent loop
            logger.exception("tool_internal_error tool=%s", name)
            result = out_model(ok=False, error_code="internal_error", error="Unexpected tool failure.")
        duration = time.monotonic() - started
        self.calls.append(ToolCallRecord(name, result.ok, result.error_code, duration, budgeted))
        logger.info("tool_call tool=%s ok=%s error_code=%s duration=%.2fs budget_left=%d",
                    name, result.ok, result.error_code, duration, self.budget_remaining)
        return result

    @staticmethod
    def _coerce(model: type[BaseModel], raw: Any) -> Any:
        if isinstance(raw, model):
            return raw
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ToolFailure("invalid_input", "Tool input must be an object.")
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            detail = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors())
            raise ToolFailure("invalid_input", detail) from exc

    def _git(self, *args: str) -> list[str]:
        return [
            "git", "-c", f"safe.directory={self._backend.workspace}", "-c", "core.quotepath=off",
            "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", *args,
        ]

    def _exec(self, argv: Sequence[str], *, timeout: int | None = None, stdin: bytes | None = None,
              ok_codes: tuple[int, ...] = (0,)) -> ExecResult:
        result = self._backend.exec(argv, timeout=timeout or self.limits.tool_timeout_seconds, stdin=stdin)
        if result.timed_out:
            raise ToolFailure("timeout", "The command exceeded the allowed time.")
        if result.exit_code not in ok_codes:
            raise ToolFailure("command_failed", result.output.strip()[-500:] or f"exit code {result.exit_code}")
        return result

    @staticmethod
    def _path(raw: str, *, allow_root: bool = False) -> str:
        try:
            return validate_repo_path(raw, allow_root=allow_root)
        except InvalidPathError as exc:
            raise ToolFailure("invalid_path", str(exc)) from exc

    # ------------------------------------------------------------ handlers
    def _load_repository(self, inp: LoadRepositoryInput) -> LoadRepositoryOutput:
        summary = self._backend.upload_repo(inp.path)
        self._baseline = None
        self._approved.clear()
        self._exec(self._git("init", "-q"))
        self._exec(self._git("config", "core.autocrlf", "false"))
        self._exec(self._git("config", "core.filemode", "false"))
        self._exec(self._git("add", "-A"))
        self._exec(self._git(
            "-c", "user.name=RepoDoctor", "-c", "user.email=repodoctor@localhost",
            "commit", "-q", "-m", "baseline", "--no-gpg-sign", "--allow-empty",
        ))
        commit = self._exec(self._git("rev-parse", "HEAD")).output.strip().splitlines()[-1]
        self._baseline = commit
        return LoadRepositoryOutput(file_count=summary.file_count, total_bytes=summary.total_bytes,
                                    baseline_commit=commit)

    def _list_files(self, inp: ListFilesInput) -> ListFilesOutput:
        path = self._path(inp.path, allow_root=True)
        res = self._exec(self._git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", path))
        files = sorted({f for f in res.output.split("\0") if f})
        cap = min(inp.max_files, self.limits.max_list_files)
        return ListFilesOutput(path=path, files=files[:cap], truncated=len(files) > cap or res.truncated)

    def _read_file(self, inp: ReadFileInput) -> ReadFileOutput:
        path = self._path(inp.path)
        lim = self.limits
        end = min(inp.end_line or inp.start_line + lim.max_read_lines - 1, inp.start_line + lim.max_read_lines - 1)
        res = self._exec(
            ["python", "-", self._backend.workspace, path, str(inp.start_line), str(end),
             str(lim.max_file_bytes), str(lim.max_line_chars)],
            stdin=READ_SCRIPT.encode("utf-8"),
        )
        try:
            data = json.loads(res.output.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise ToolFailure("command_failed", "Unexpected output while reading the file.") from exc
        errors = {
            "not_found": ("not_found", f"File not found: {path}"),
            "outside_workspace": ("invalid_path", "Path resolves outside the repository."),
            "file_too_large": ("file_too_large", f"File is larger than {lim.max_file_bytes} bytes."),
            "binary_file": ("binary_file", "Binary files cannot be read."),
        }
        if "error" in data:
            code, message = errors.get(data["error"], ("command_failed", str(data["error"])))
            raise ToolFailure(code, message)
        chosen = data["lines"]
        last = inp.start_line + len(chosen) - 1
        return ReadFileOutput(
            path=path, start_line=inp.start_line, end_line=last, total_lines=data["total_lines"],
            content="\n".join(chosen), has_more=last < data["total_lines"],
            long_lines_cut=bool(data.get("long_lines_cut")),
        )

    def _search_code(self, inp: SearchCodeInput) -> SearchCodeOutput:
        path = self._path(inp.path, allow_root=True)
        flags = ["-n", "-z", "-I", "--no-color", "--untracked", "-F" if inp.literal else "-E"]
        if not inp.case_sensitive:
            flags.append("-i")
        res = self._exec(self._git("grep", *flags, "-e", inp.pattern, "--", path), ok_codes=(0, 1))
        cap = min(inp.max_results, self.limits.max_search_results)
        matches: list[SearchMatch] = []
        total = 0
        for record in res.output.split("\n"):
            parts = record.split("\0", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            total += 1
            if len(matches) < cap:
                matches.append(SearchMatch(path=parts[0], line=int(parts[1]),
                                           text=parts[2][: self.limits.max_search_line_chars].rstrip("\r")))
        return SearchCodeOutput(matches=matches, truncated=total > cap or res.truncated)

    def _run_tests(self, inp: RunTestsInput) -> RunTestsOutput:
        res = self._backend.exec(PYTEST_ARGV, timeout=inp.timeout_seconds)
        parsed = parse_pytest_output(res.output)
        status = classify_run(res.exit_code, parsed, res.timed_out)
        counts = Counts(**vars(parsed.counts)) if parsed else Counts()
        failed = [FailedTest(test_id=f.test_id, kind=f.kind, message=f.message) for f in parsed.failures] if parsed else []
        excerpt = res.output[-self.limits.output_excerpt_chars:]
        if len(res.output) > self.limits.output_excerpt_chars and "\n" in excerpt:
            excerpt = excerpt.split("\n", 1)[1]
        return RunTestsOutput(
            status=status, exit_code=res.exit_code, timed_out=res.timed_out, counts=counts,
            failed_tests=failed, duration_seconds=res.duration_seconds,
            summary=_summarize(status, counts), output_excerpt=excerpt, output_truncated=res.truncated,
        )

    def _diff_stats(self) -> tuple[list[str], int, int]:
        res = self._exec(self._git("diff", "HEAD", "--numstat"))
        files: list[str] = []
        ins = dele = 0
        for line in res.output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            files.append(parts[2])
            ins += int(parts[0]) if parts[0].isdigit() else 0
            dele += int(parts[1]) if parts[1].isdigit() else 0
        return files, ins, dele

    def _git_diff(self, inp: GitDiffInput) -> GitDiffOutput:
        res = self._exec(self._git("diff", "HEAD", "--no-color", "--no-ext-diff", "--no-textconv"))
        if res.truncated:
            raise ToolFailure("diff_too_large", "The diff is too large to display safely.")
        files, ins, dele = self._diff_stats()
        return GitDiffOutput(has_changes=bool(res.output.strip()), diff=res.output, files_changed=files,
                             insertions=ins, deletions=dele)

    def _validated(self, diff: str):
        try:
            return validate_patch(diff, self.limits)
        except PatchRejected as exc:
            raise ToolFailure(exc.code, str(exc)) from exc

    def _git_apply(self, patch_text: str, *, check: bool) -> None:
        argv = self._git("apply", *(["--check"] if check else []), "--recount", "--whitespace=nowarn", "-")
        res = self._backend.exec(argv, timeout=self.limits.tool_timeout_seconds, stdin=patch_text.encode("utf-8"))
        if res.timed_out or res.exit_code != 0:
            raise ToolFailure("patch_does_not_apply", res.output.strip()[-500:] or "git apply failed")

    def _check_patch(self, inp: CheckPatchInput) -> CheckPatchOutput:
        patch = self._validated(inp.diff)
        self._git_apply(patch.diff, check=True)
        return CheckPatchOutput(sha256=patch.sha256, diff=patch.diff, files=list(patch.files),
                                insertions=patch.insertions, deletions=patch.deletions)

    def _apply_patch(self, inp: ApplyPatchInput) -> ApplyPatchOutput:
        patch = self._validated(inp.diff)
        if patch.sha256 not in self._approved:
            raise ToolFailure("approval_required", "This exact patch has not been approved.")
        self._git_apply(patch.diff, check=True)
        self._git_apply(patch.diff, check=False)
        self._approved.discard(patch.sha256)  # approvals are single-use
        return ApplyPatchOutput(sha256=patch.sha256, files_changed=list(patch.files),
                                insertions=patch.insertions, deletions=patch.deletions)

    def _reset_changes(self, inp: ResetChangesInput) -> ResetChangesOutput:
        self._exec(self._git("reset", "-q", "--hard", "HEAD"))
        self._exec(self._git("clean", "-fdq"))
        status = self._exec(self._git("status", "--porcelain"))
        return ResetChangesOutput(clean=not status.output.strip())


def _summarize(status: str, counts: Counts) -> str:
    if status == "timeout":
        return "Test execution exceeded the allowed time."
    if status == "unparseable":
        return "Could not interpret the test output; treating this run as NOT verified."
    if status == "no_tests":
        return "No tests ran, so nothing was verified."
    parts = [f"{n} {label}" for label, n in (
        ("failed", counts.failed), ("errors", counts.errors), ("passed", counts.passed),
        ("skipped", counts.skipped)) if n]
    return ", ".join(parts) or "No results."
