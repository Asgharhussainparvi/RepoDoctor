"""Docker sandbox: the only execution boundary in RepoDoctor."""
from .config import SandboxConfig
from .errors import (
    DockerUnavailableError,
    SandboxError,
    SandboxImageMissingError,
    SandboxNotRunningError,
    UploadRejectedError,
)
from .manager import SandboxManager
from .models import ExecResult, UploadSummary

__all__ = [
    "DockerUnavailableError", "ExecResult", "SandboxConfig", "SandboxError",
    "SandboxImageMissingError", "SandboxManager", "SandboxNotRunningError",
    "UploadRejectedError", "UploadSummary",
]
