"""Patch normalisation and policy validation. Pure functions, no I/O.

Policy (MVP): a patch may only MODIFY existing files, must use a/ and b/ prefixes,
may not touch tests or test configuration, and must stay small. Anything else is
rejected before it gets anywhere near `git apply`.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .limits import ToolLimits
from .paths import InvalidPathError, is_protected_path, validate_repo_path

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_HEADER_REJECTS = (
    (re.compile(r"^(rename|copy) (from|to) "), "rename_not_allowed", "Renames and copies are not allowed."),
    (re.compile(r"^similarity index "), "rename_not_allowed", "Renames and copies are not allowed."),
    (re.compile(r"^(old|new) mode "), "mode_change_not_allowed", "File mode changes are not allowed."),
    (re.compile(r"^new file mode "), "file_creation_not_allowed", "Creating files is not allowed."),
    (re.compile(r"^deleted file mode "), "file_deletion_not_allowed", "Deleting files is not allowed."),
    (re.compile(r"^(GIT binary patch|Binary files )"), "binary_patch_not_allowed", "Binary patches are not allowed."),
)


class PatchRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedPatch:
    diff: str  # normalised text; this exact string is what gets hashed and applied
    sha256: str
    files: tuple[str, ...]
    insertions: int
    deletions: int


def patch_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def normalize_patch(diff: str) -> str:
    """Strip a surrounding markdown fence and blank edge lines; end with one newline."""
    lines = diff.replace("\ufeff", "").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines) + "\n" if lines else ""


def _header_path(token: str, prefix: str, *, old_side: bool, limits: ToolLimits) -> str:
    token = token.split("\t")[0].rstrip("\r")
    if token == "/dev/null":
        if old_side:
            raise PatchRejected("file_creation_not_allowed", "Creating files is not allowed.")
        raise PatchRejected("file_deletion_not_allowed", "Deleting files is not allowed.")
    if token.startswith('"'):
        raise PatchRejected("invalid_path", "Quoted paths are not supported.")
    if not token.startswith(prefix):
        raise PatchRejected("invalid_path", f"Patch paths must start with '{prefix}'.")
    try:
        path = validate_repo_path(token[len(prefix):])
    except InvalidPathError as exc:
        raise PatchRejected("invalid_path", f"Unsafe path in patch: {exc}") from exc
    if not limits.allow_test_edits and is_protected_path(path):
        raise PatchRejected(
            "protected_path", f"Editing test or test-configuration file '{path}' is not allowed."
        )
    return path


def validate_patch(diff: str, limits: ToolLimits | None = None) -> ValidatedPatch:
    limits = limits or ToolLimits()
    if not isinstance(diff, str) or not diff.strip():
        raise PatchRejected("empty_patch", "The patch is empty.")
    if len(diff.encode("utf-8")) > limits.max_patch_bytes:
        raise PatchRejected("patch_too_large", f"The patch exceeds {limits.max_patch_bytes} bytes.")
    if "\x00" in diff:
        raise PatchRejected("malformed_patch", "The patch contains NUL bytes.")

    text = normalize_patch(diff)
    lines = text.split("\n")[:-1]
    n = len(lines)
    files: list[str] = []
    current: str | None = None
    in_hunk = False
    insertions = deletions = 0
    i = 0
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            in_hunk, current = False, None
            i += 1
            continue
        if line.startswith("--- ") and i + 2 < n and lines[i + 1].startswith("+++ ") and lines[i + 2].startswith("@@"):
            old = _header_path(line[4:], "a/", old_side=True, limits=limits)
            new = _header_path(lines[i + 1][4:], "b/", old_side=False, limits=limits)
            if old != new:
                raise PatchRejected("rename_not_allowed", "Renames are not allowed.")
            current = old
            if old not in files:
                files.append(old)
            in_hunk = False
            i += 2  # now pointing at the first @@ line
            continue
        if line.startswith("@@"):
            if current is None or not _HUNK_RE.match(line):
                raise PatchRejected("malformed_patch", "Malformed hunk header.")
            in_hunk = True
            i += 1
            continue
        if in_hunk:
            first = line[:1]
            if first == "+":
                insertions += 1
            elif first == "-":
                deletions += 1
            elif first not in (" ", "\\") and line != "":
                raise PatchRejected("malformed_patch", f"Unexpected line inside a hunk: {line[:60]!r}")
            i += 1
            continue
        for regex, code, message in _HEADER_REJECTS:
            if regex.match(line):
                raise PatchRejected(code, message)
        if line.startswith("index "):
            i += 1
            continue
        raise PatchRejected("malformed_patch", f"Unexpected line outside a hunk: {line[:60]!r}")

    if not files:
        raise PatchRejected("malformed_patch", "No file changes found. Use a unified diff with a/ and b/ paths.")
    if len(files) > limits.max_patch_files:
        raise PatchRejected("too_many_files", f"The patch touches more than {limits.max_patch_files} files.")
    if insertions + deletions > limits.max_patch_changed_lines:
        raise PatchRejected(
            "patch_too_large", f"The patch changes more than {limits.max_patch_changed_lines} lines."
        )
    return ValidatedPatch(text, patch_sha256(text), tuple(files), insertions, deletions)
