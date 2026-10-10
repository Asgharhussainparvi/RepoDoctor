"""RepoDoctor tools: typed, validated, logged operations over the sandbox."""
from .limits import ToolLimits
from .patch import PatchRejected, ValidatedPatch, normalize_patch, patch_sha256, validate_patch
from .paths import InvalidPathError, is_protected_path, validate_repo_path
from .pytest_parser import RunStatus, classify_run, parse_pytest_output
from .toolbox import ExecutionBackend, Toolbox, ToolCallRecord

__all__ = [
    "ExecutionBackend", "InvalidPathError", "PatchRejected", "RunStatus", "ToolCallRecord", "ToolLimits",
    "Toolbox", "ValidatedPatch", "classify_run", "is_protected_path", "normalize_patch",
    "parse_pytest_output", "patch_sha256", "validate_patch", "validate_repo_path",
]
