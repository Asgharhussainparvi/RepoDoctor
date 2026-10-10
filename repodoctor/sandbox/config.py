"""Sandbox configuration with validation. Stdlib only."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

_SIZE_RE = re.compile(r"^[1-9][0-9]*[kmg]$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$")


@dataclass(frozen=True)
class SandboxConfig:
    image: str = "repodoctor-sandbox:0.1"
    docker_bin: str = "docker"
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 256
    workspace_size: str = "256m"
    tmp_size: str = "64m"
    default_timeout_seconds: int = 60
    max_timeout_seconds: int = 300
    max_output_bytes: int = 1_000_000
    max_upload_bytes: int = 50_000_000
    max_upload_files: int = 5000

    def __post_init__(self) -> None:
        if not _IMAGE_RE.match(self.image):
            raise ValueError(f"invalid image name: {self.image!r}")
        if not self.docker_bin:
            raise ValueError("docker_bin must not be empty")
        for name in ("memory", "workspace_size", "tmp_size"):
            if not _SIZE_RE.match(getattr(self, name)):
                raise ValueError(f"{name} must look like '512m' or '1g', got {getattr(self, name)!r}")
        if not 0 < self.cpus <= 8:
            raise ValueError("cpus must be in (0, 8]")
        if self.pids_limit < 16:
            raise ValueError("pids_limit must be at least 16")
        if self.default_timeout_seconds < 1:
            raise ValueError("default_timeout_seconds must be >= 1")
        if self.max_timeout_seconds < self.default_timeout_seconds:
            raise ValueError("max_timeout_seconds must be >= default_timeout_seconds")
        if self.max_output_bytes < 100:
            raise ValueError("max_output_bytes must be >= 100")
        if self.max_upload_bytes < 1 or self.max_upload_files < 1:
            raise ValueError("upload limits must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SandboxConfig":
        """Build config from REPODOCTOR_* variables; empty values count as unset."""
        source = os.environ if env is None else env
        mapping = {
            "REPODOCTOR_SANDBOX_IMAGE": ("image", str),
            "REPODOCTOR_SANDBOX_MEMORY": ("memory", str),
            "REPODOCTOR_SANDBOX_CPUS": ("cpus", float),
            "REPODOCTOR_TEST_TIMEOUT_SECONDS": ("default_timeout_seconds", int),
            "REPODOCTOR_MAX_TIMEOUT_SECONDS": ("max_timeout_seconds", int),
            "REPODOCTOR_MAX_OUTPUT_BYTES": ("max_output_bytes", int),
        }
        values: dict = {}
        for var, (field_name, cast) in mapping.items():
            raw = source.get(var, "").strip()
            if not raw:
                continue
            try:
                values[field_name] = cast(raw)
            except ValueError as exc:
                raise ValueError(f"{var} has an invalid value: {raw!r}") from exc
        return cls(**values)
