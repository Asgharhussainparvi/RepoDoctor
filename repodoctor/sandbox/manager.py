"""SandboxManager: the ONLY module in RepoDoctor allowed to execute processes.

Everything untrusted (repository code, tests, git on a repository) runs inside a
locked-down Docker container that this module creates and destroys. Nothing here
uses a shell: every command is an argv list.
"""
from __future__ import annotations

import io
import logging
import os
import re
import subprocess
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import SandboxConfig
from .errors import (
    DOCKER_UNAVAILABLE_MESSAGE,
    DockerUnavailableError,
    SandboxError,
    SandboxImageMissingError,
    SandboxNotRunningError,
    UploadRejectedError,
)
from .models import ExecResult, UploadSummary

logger = logging.getLogger("repodoctor.sandbox")

WORKSPACE = "/workspace"
MANAGED_LABEL = "repodoctor.managed=true"
KILL_GRACE_SECONDS = 2
BACKSTOP_SECONDS = 15
TRUNCATION_MARKER = b"\n...[output truncated]...\n"
EXCLUDED_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv", "venv", "node_modules"}
)
MAX_ARGV_ITEMS = 64
MAX_STDIN_BYTES = 1_000_000
MAX_ARG_LENGTH = 4096
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_DOCKER_DAEMON_ERROR_RCS = (1, 125, 126, 127)


# --------------------------------------------------------------------------
# Low-level process runner (the single place that touches `subprocess`)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _RawResult:
    returncode: int | None
    data: bytes
    truncated: bool
    timed_out: bool
    duration: float


def _run_process(
    cmd: Sequence[str], *, input_bytes: bytes | None, timeout: float, max_bytes: int
) -> _RawResult:
    """Run cmd (no shell). Output is bounded: first half + last half are kept.

    Keeping the tail matters: the pytest summary line is printed last.
    """
    started = time.monotonic()
    proc = subprocess.Popen(
        list(cmd),
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    head_cap = max_bytes // 2
    tail_cap = max_bytes - head_cap
    head = bytearray()
    tail = bytearray()
    total = [0]

    def drain() -> None:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            total[0] += len(chunk)
            if len(head) < head_cap:
                take = head_cap - len(head)
                head.extend(chunk[:take])
                chunk = chunk[take:]
            if chunk:
                tail.extend(chunk)
                if len(tail) > tail_cap:
                    del tail[: len(tail) - tail_cap]

    def feed() -> None:
        assert proc.stdin is not None and input_bytes is not None
        try:
            proc.stdin.write(input_bytes)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=drain, daemon=True)]
    if input_bytes is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    for t in threads:
        t.join(timeout=5)

    truncated = total[0] > len(head) + len(tail)
    data = bytes(head) + (TRUNCATION_MARKER if truncated else b"") + bytes(tail)
    return _RawResult(
        returncode=None if timed_out else proc.returncode,
        data=data,
        truncated=truncated,
        timed_out=timed_out,
        duration=time.monotonic() - started,
    )


def _docker_call(
    config: SandboxConfig,
    args: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    timeout: float,
    max_bytes: int | None = None,
) -> _RawResult:
    try:
        return _run_process(
            [config.docker_bin, *args],
            input_bytes=input_bytes,
            timeout=timeout,
            max_bytes=max_bytes or config.max_output_bytes,
        )
    except OSError as exc:
        raise DockerUnavailableError(f"{DOCKER_UNAVAILABLE_MESSAGE} ({exc})") from exc


def _text(raw: _RawResult) -> str:
    return raw.data.decode("utf-8", errors="replace")


def _tail(raw: _RawResult, limit: int = 500) -> str:
    return _text(raw).strip()[-limit:]


# --------------------------------------------------------------------------
# Pure helpers (unit-tested without Docker)
# --------------------------------------------------------------------------
def is_timeout_exit(exit_code: int | None, duration: float, timeout: int) -> bool:
    """Interpret `timeout(1)` exit codes: 124 = TERM worked, 137 = KILL was needed.

    137 is also what an out-of-memory kill looks like, so require the elapsed
    time to have reached the limit.
    """
    if exit_code == 124:
        return True
    return exit_code == 137 and duration >= timeout


def validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, (list, tuple)):
        raise ValueError("argv must be a list of strings")
    if not argv or len(argv) > MAX_ARGV_ITEMS:
        raise ValueError(f"argv must contain 1..{MAX_ARGV_ITEMS} items")
    for item in argv:
        if not isinstance(item, str):
            raise ValueError("argv items must be strings")
        if "\x00" in item or len(item) > MAX_ARG_LENGTH:
            raise ValueError("argv item is invalid or too long")
    if not argv[0]:
        raise ValueError("argv[0] must not be empty")
    return tuple(argv)


def build_repo_tar(source: Path, config: SandboxConfig) -> tuple[bytes, UploadSummary]:
    """Pack a repository directory into an in-memory tar.

    Skips VCS/cache/virtualenv directories and never follows or copies symlinks.
    Ownership is normalised and modes are reduced to 0644/0755.
    """
    root = Path(source)
    if not root.is_dir():
        raise UploadRejectedError(f"Not a directory: {root}")
    root = root.resolve()
    buf = io.BytesIO()
    files = total = skipped = 0
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            kept = []
            for name in sorted(dirnames):
                if name in EXCLUDED_NAMES:
                    continue
                if (Path(dirpath) / name).is_symlink():
                    skipped += 1
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in sorted(filenames):
                if name == ".git":
                    continue
                full = Path(dirpath) / name
                if full.is_symlink() or not full.is_file():
                    skipped += 1
                    continue
                files += 1
                total += full.stat().st_size
                if files > config.max_upload_files:
                    raise UploadRejectedError(f"Repository has more than {config.max_upload_files} files")
                if total > config.max_upload_bytes:
                    raise UploadRejectedError(f"Repository is larger than {config.max_upload_bytes} bytes")
                info = tar.gettarinfo(str(full), arcname=full.relative_to(root).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o755 if info.mode & 0o111 else 0o644
                with full.open("rb") as handle:
                    tar.addfile(info, handle)
    if files == 0:
        raise UploadRejectedError("Repository contains no files")
    return buf.getvalue(), UploadSummary(file_count=files, total_bytes=total, skipped_links=skipped)


# --------------------------------------------------------------------------
# SandboxManager
# --------------------------------------------------------------------------
class SandboxManager:
    """One container per debugging session. Use as a context manager."""

    def __init__(self, config: SandboxConfig | None = None, session_id: str | None = None) -> None:
        self.config = config or SandboxConfig()
        sid = uuid.uuid4().hex[:12] if session_id is None else session_id
        if not _SESSION_RE.fullmatch(sid):
            raise ValueError("session_id must match [A-Za-z0-9_-]{1,40}")
        self.session_id = sid
        self.container_name = f"repodoctor-{sid}"
        self._state = "created"  # created -> running -> destroyed | dead

    # ---- environment checks (static: usable before any session exists) ----
    @staticmethod
    def check_docker(config: SandboxConfig) -> None:
        raw = _docker_call(config, ["info", "--format", "{{.OSType}}"], timeout=20)
        lines = [ln.strip() for ln in _text(raw).splitlines() if ln.strip()]
        if raw.timed_out or raw.returncode != 0 or not lines:
            raise DockerUnavailableError(f"{DOCKER_UNAVAILABLE_MESSAGE} ({_tail(raw)})")
        if lines[-1] != "linux":
            raise SandboxError(f"Sandbox needs Linux containers, but Docker reports {lines[-1]!r}.")

    @staticmethod
    def image_exists(config: SandboxConfig) -> bool:
        raw = _docker_call(config, ["image", "inspect", config.image], timeout=20)
        return not raw.timed_out and raw.returncode == 0

    @staticmethod
    def build_image(config: SandboxConfig, dockerfile: Path | None = None) -> str:
        root = Path(__file__).resolve().parents[2]
        dockerfile = dockerfile or root / "docker" / "sandbox.Dockerfile"
        raw = _docker_call(
            config,
            ["build", "-t", config.image, "-f", str(dockerfile), str(dockerfile.parent)],
            timeout=900,
            max_bytes=5_000_000,
        )
        if raw.timed_out or raw.returncode != 0:
            raise SandboxError(f"Image build failed: {_tail(raw, 1500)}")
        return _text(raw)

    @staticmethod
    def cleanup_stale(config: SandboxConfig) -> int:
        """Remove every RepoDoctor container (including from crashed sessions)."""
        raw = _docker_call(config, ["ps", "-aq", "--filter", f"label={MANAGED_LABEL}"], timeout=20)
        ids = _text(raw).split()
        if raw.returncode != 0:
            raise DockerUnavailableError(f"{DOCKER_UNAVAILABLE_MESSAGE} ({_tail(raw)})")
        if ids:
            _docker_call(config, ["rm", "-f", "-v", *ids], timeout=60)
        return len(ids)

    # ---- container configuration ----
    def build_run_args(self) -> list[str]:
        c = self.config
        return [
            "run", "-d", "--init",
            "--name", self.container_name,
            "--label", MANAGED_LABEL,
            "--label", f"repodoctor.session={self.session_id}",
            "--network", "none",
            "--memory", c.memory, "--memory-swap", c.memory,
            "--cpus", str(c.cpus),
            "--pids-limit", str(c.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={c.tmp_size}",
            "--tmpfs", f"{WORKSPACE}:rw,exec,nosuid,nodev,mode=1777,size={c.workspace_size}",
            "-w", WORKSPACE,
            c.image,
            "sleep", "infinity",
        ]

    @property
    def workspace(self) -> str:
        """Absolute path of the repository inside the container."""
        return WORKSPACE

    # ---- lifecycle ----
    def start(self) -> None:
        if self._state != "created":
            raise SandboxError(f"Cannot start a sandbox that is {self._state}")
        self.check_docker(self.config)
        if not self.image_exists(self.config):
            raise SandboxImageMissingError(
                f"Sandbox image {self.config.image!r} not found. Run: python -m repodoctor.sandbox build"
            )
        raw = self._docker(self.build_run_args(), timeout=60)
        if raw.timed_out or raw.returncode != 0:
            self._docker(["rm", "-f", "-v", self.container_name], timeout=30)
            raise SandboxError(f"Failed to start sandbox container: {_tail(raw)}")
        self._state = "running"
        logger.info("sandbox_started session=%s container=%s", self.session_id, self.container_name)

    def upload_repo(self, source: str | Path) -> UploadSummary:
        self._require_running()
        data, summary = build_repo_tar(Path(source), self.config)
        raw = self._docker(
            ["exec", "-i", "-w", WORKSPACE, self.container_name, "tar", "-x", "-f", "-", "-C", WORKSPACE],
            input_bytes=data,
            timeout=120,
        )
        if raw.timed_out or raw.returncode != 0:
            raise SandboxError(f"Failed to copy repository into sandbox: {_tail(raw)}")
        logger.info(
            "sandbox_upload session=%s files=%d bytes=%d skipped_links=%d",
            self.session_id, summary.file_count, summary.total_bytes, summary.skipped_links,
        )
        return summary

    def exec(
        self, argv: Sequence[str], timeout: int | None = None, stdin: bytes | None = None
    ) -> ExecResult:
        """Run argv (no shell) inside the sandbox with a hard time limit.

        `stdin` (optional, bytes) is piped to the process, e.g. a patch for `git apply -`.
        """
        self._require_running()
        checked = validate_argv(argv)
        limit = self._resolve_timeout(timeout)
        if stdin is not None and (not isinstance(stdin, bytes) or len(stdin) > MAX_STDIN_BYTES):
            raise ValueError(f"stdin must be bytes of at most {MAX_STDIN_BYTES} bytes")
        cmd = [
            "exec", *(["-i"] if stdin is not None else []), "-w", WORKSPACE, self.container_name,
            "timeout", "-s", "TERM", "-k", str(KILL_GRACE_SECONDS), str(limit), *checked,
        ]
        raw = self._docker(cmd, input_bytes=stdin, timeout=limit + BACKSTOP_SECONDS)
        output = _text(raw)
        if raw.timed_out:
            # Backstop: the in-container timeout did not fire. Kill the whole sandbox.
            self._docker(["kill", self.container_name], timeout=15)
            self._state = "dead"
            result = ExecResult(checked, None, output, True, raw.truncated, raw.duration)
        else:
            if raw.returncode in _DOCKER_DAEMON_ERROR_RCS and output.lstrip().startswith("Error response from daemon"):
                self._state = "dead"
                raise SandboxNotRunningError(f"Sandbox container is not usable: {output.strip()[-300:]}")
            timed_out = is_timeout_exit(raw.returncode, raw.duration, limit)
            result = ExecResult(checked, raw.returncode, output, timed_out, raw.truncated, raw.duration)
        logger.info(
            "sandbox_exec session=%s argv0=%s exit=%s timed_out=%s truncated=%s duration=%.2fs",
            self.session_id, checked[0], result.exit_code, result.timed_out, result.truncated,
            result.duration_seconds,
        )
        return result

    def container_present(self) -> bool:
        raw = self._docker(["container", "inspect", self.container_name], timeout=20)
        return not raw.timed_out and raw.returncode == 0

    def destroy(self) -> None:
        """Remove the container and its tmpfs data. Safe to call repeatedly."""
        if self._state in ("destroyed", "created"):
            self._state = "destroyed"
            return
        try:
            raw = self._docker(["rm", "-f", "-v", self.container_name], timeout=60)
        except SandboxError as exc:
            logger.error("sandbox_destroy_failed session=%s error=%s", self.session_id, exc)
            return
        if raw.returncode == 0 or "No such container" in _text(raw):
            self._state = "destroyed"
            logger.info("sandbox_destroyed session=%s", self.session_id)
        else:
            logger.error("sandbox_destroy_failed session=%s output=%s", self.session_id, _tail(raw))

    def __enter__(self) -> "SandboxManager":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.destroy()

    # ---- internals ----
    def _docker(self, args: Sequence[str], *, input_bytes: bytes | None = None, timeout: float) -> _RawResult:
        return _docker_call(self.config, args, input_bytes=input_bytes, timeout=timeout)

    def _require_running(self) -> None:
        if self._state != "running":
            raise SandboxNotRunningError(f"Sandbox is {self._state}, not running")

    def _resolve_timeout(self, timeout: int | None) -> int:
        value = self.config.default_timeout_seconds if timeout is None else timeout
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= self.config.max_timeout_seconds:
            raise ValueError(f"timeout must be an integer between 1 and {self.config.max_timeout_seconds} seconds")
        return value
