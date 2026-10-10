"""Repository path validation and the protected-path policy. Pure functions."""
from __future__ import annotations

import fnmatch
import re

MAX_PATH_LENGTH = 300
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

PROTECTED_DIR_NAMES = frozenset({"tests", "test", "__tests__"})
PROTECTED_FILE_NAMES = frozenset(
    {"conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "noxfile.py", ".coveragerc"}
)
PROTECTED_FILE_GLOBS = ("test_*.py", "*_test.py")


class InvalidPathError(ValueError):
    """The path is unsafe or malformed."""


def validate_repo_path(raw: str, *, allow_root: bool = False) -> str:
    """Return a normalised repo-relative POSIX path, or raise InvalidPathError."""
    if not isinstance(raw, str):
        raise InvalidPathError("path must be a string")
    if raw != raw.strip():
        raise InvalidPathError("path must not start or end with whitespace")
    if len(raw) > MAX_PATH_LENGTH:
        raise InvalidPathError("path is too long")
    if _CONTROL_RE.search(raw):
        raise InvalidPathError("path contains control characters")
    if "\\" in raw:
        raise InvalidPathError("use forward slashes in paths")
    if raw.startswith("/") or _DRIVE_RE.match(raw):
        raise InvalidPathError("absolute paths are not allowed")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise InvalidPathError("'..' is not allowed in paths")
    if any(p.lower() == ".git" for p in parts):
        raise InvalidPathError("the .git directory is off limits")
    if parts and parts[0].startswith("-"):
        raise InvalidPathError("paths must not start with '-'")
    if not parts:
        if allow_root:
            return "."
        raise InvalidPathError("path must not be empty")
    return "/".join(parts)


def is_protected_path(path: str) -> bool:
    """True for test files and test configuration (editing them would fake a fix)."""
    parts = [p.lower() for p in path.split("/")]
    name = parts[-1]
    if any(p in PROTECTED_DIR_NAMES for p in parts[:-1]):
        return True
    if name in PROTECTED_FILE_NAMES:
        return True
    return any(fnmatch.fnmatchcase(name, g) for g in PROTECTED_FILE_GLOBS)
