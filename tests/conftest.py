"""Shared test helpers.

LocalSandbox is a TEST DOUBLE for SandboxManager: same interface, but it runs
commands in a temp directory on the host so tool logic can be tested with real
git and real pytest without Docker. Only ever use it with our trusted fixture.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repodoctor.sandbox.models import ExecResult, UploadSummary

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "buggy_payment_app"
EXPECTED_PATCH = ROOT / "fixtures" / "expected_patches" / "buggy_payment_app.diff"
FAILING_TEST = "tests/test_payment.py::test_charge_records_receipt_for_customer"


def _bug_line() -> int:
    """Line of the intentional bug. Computed, because an editor/formatter may legitimately shift it."""
    source = (FIXTURE / "app" / "payment.py").read_text(encoding="utf-8")
    for number, line in enumerate(source.splitlines(), start=1):
        if "charge(user_id" in line:
            return number
    raise RuntimeError(
        "the fixture no longer contains the intentional bug (charge(user_id, ...))"
    )


BUG_LINE = _bug_line()


class LocalSandbox:
    def __init__(self, workspace: Path) -> None:
        self.workspace = str(workspace)
        self.started = False
        self.destroyed = False

    def start(self):
        self.started = True

    def destroy(self):
        self.destroyed = True

    def upload_repo(self, source):
        from repodoctor.sandbox import SandboxConfig
        from repodoctor.sandbox.manager import build_repo_tar

        build_repo_tar(
            Path(source), SandboxConfig()
        )  # same validation as the real sandbox (empty/missing dirs)
        ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache")
        shutil.copytree(source, self.workspace, ignore=ignore, dirs_exist_ok=True)
        files = [p for p in Path(self.workspace).rglob("*") if p.is_file()]
        return UploadSummary(len(files), sum(p.stat().st_size for p in files), 0)

    def exec(self, argv, timeout=None, stdin=None):
        argv = [
            sys.executable if i == 0 and a == "python" else a
            for i, a in enumerate(argv)
        ]
        env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "GIT_CONFIG_GLOBAL": os.devnull,  # ignore the developer's own git config
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=self.workspace,
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout or 60,
                env=env,
                check=False,
            )
            code, out, timed_out = proc.returncode, proc.stdout, False
        except subprocess.TimeoutExpired as exc:
            code, out, timed_out = None, exc.stdout or b"", True
        return ExecResult(
            tuple(argv),
            code,
            out.decode("utf-8", errors="replace"),
            timed_out,
            False,
            time.monotonic() - started,
        )


@pytest.fixture
def local_sandbox(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return LocalSandbox(workspace)


@pytest.fixture
def make_toolbox(local_sandbox):
    from repodoctor.tools import Toolbox, ToolLimits

    def factory(limits=None, load=True):
        toolbox = Toolbox(local_sandbox, limits or ToolLimits())
        if load:
            loaded = toolbox.load_repository({"path": str(FIXTURE)})
            assert loaded.ok, loaded.error
        return toolbox

    return factory


@pytest.fixture
def toolbox(make_toolbox):
    return make_toolbox()


@pytest.fixture
def expected_diff():
    return EXPECTED_PATCH.read_text(encoding="utf-8")


class SandboxFactory:
    """Creates a fresh LocalSandbox per call and remembers them (to assert cleanup)."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.created: list[LocalSandbox] = []

    def __call__(self) -> LocalSandbox:
        workspace = self.base / f"ws{len(self.created)}"
        workspace.mkdir(parents=True)
        sandbox = LocalSandbox(workspace)
        self.created.append(sandbox)
        return sandbox


@pytest.fixture
def sandbox_factory(tmp_path):
    return SandboxFactory(tmp_path / "sandboxes")


@pytest.fixture
def wrong_diff(expected_diff):
    """Applies cleanly but does NOT fix the bug (still a NameError)."""
    return expected_diff.replace("customer_id, amount_cents", "other_id, amount_cents")


@pytest.fixture
def make_orchestrator(tmp_path, sandbox_factory):
    from repodoctor.agent import AgentConfig, Orchestrator, SessionStore

    def factory(
        engine, *, store=None, factory=None, config=None, tool_limits=None, clock=None
    ):
        kwargs = {"clock": clock} if clock else {}
        return Orchestrator(
            store or SessionStore(),
            engine,
            factory or sandbox_factory,
            config=config or AgentConfig(),
            tool_limits=tool_limits,
            **kwargs,
        )

    return factory
