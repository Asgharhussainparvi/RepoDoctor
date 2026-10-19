"""API settings, read from the process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(",") if p.strip())


@dataclass(frozen=True)
class ApiSettings:
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000
    engine: str = "bedrock"  # "bedrock" (real model) or "demo" (scripted patch, sample repository only)
    max_active_sessions: int = (
        2  # each active session may hold a container (512 MB by default)
    )
    max_upload_bytes: int = 20_000_000
    max_unpacked_bytes: int = 100_000_000
    max_repo_files: int = 5000
    waiting_ttl_seconds: int = 1800
    janitor_interval_seconds: float = 60.0
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]")

    def __post_init__(self) -> None:
        if self.engine not in ("bedrock", "demo"):
            raise ValueError("REPODOCTOR_ENGINE must be 'bedrock' or 'demo'")
        for name in (
            "max_active_sessions",
            "max_upload_bytes",
            "max_unpacked_bytes",
            "max_repo_files",
            "waiting_ttl_seconds",
            "port",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ApiSettings":
        source = os.environ if env is None else env
        values: dict = {}
        for var, name, cast in (
            ("REPODOCTOR_DATA_DIR", "data_dir", Path),
            ("REPODOCTOR_HOST", "host", str),
            ("REPODOCTOR_PORT", "port", int),
            ("REPODOCTOR_ENGINE", "engine", str),
            ("REPODOCTOR_MAX_ACTIVE_SESSIONS", "max_active_sessions", int),
            ("REPODOCTOR_MAX_UPLOAD_BYTES", "max_upload_bytes", int),
            ("REPODOCTOR_MAX_UNPACKED_BYTES", "max_unpacked_bytes", int),
            ("REPODOCTOR_MAX_REPO_FILES", "max_repo_files", int),
            ("REPODOCTOR_WAITING_TTL_SECONDS", "waiting_ttl_seconds", int),
            ("REPODOCTOR_CORS_ORIGINS", "cors_origins", _csv),
            ("REPODOCTOR_ALLOWED_HOSTS", "allowed_hosts", _csv),
        ):
            raw = source.get(var, "").strip()
            if raw:
                try:
                    values[name] = cast(raw)
                except ValueError as exc:
                    raise ValueError(f"{var} has an invalid value: {raw!r}") from exc
        return cls(**values)
