"""Sandbox error types. Messages are safe to show in the UI."""

from __future__ import annotations

DOCKER_UNAVAILABLE_MESSAGE = "Sandbox unavailable. Docker must be running."


class SandboxError(Exception):
    """Base class for all sandbox failures."""


class DockerUnavailableError(SandboxError):
    """Docker is missing, stopped, or not reachable.

    The message is user-facing and always clean; technical detail goes to `.detail` and the logs.
    """

    def __init__(
        self, message: str = DOCKER_UNAVAILABLE_MESSAGE, detail: str = ""
    ) -> None:
        super().__init__(message)
        self.detail = detail


class SandboxImageMissingError(SandboxError):
    """The sandbox image has not been built yet."""


class SandboxNotRunningError(SandboxError):
    """An operation needs a running container but there is none."""


class UploadRejectedError(SandboxError):
    """The repository cannot be copied into the sandbox."""
