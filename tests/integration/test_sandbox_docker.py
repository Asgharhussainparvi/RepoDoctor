"""Real Docker tests. Skipped if Docker is down; FAIL if the image is not built.

Build the image first:  python -m repodoctor.sandbox build
Run only these:         python -m pytest -m docker -v
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager
from repodoctor.sandbox.manager import TRUNCATION_MARKER

pytestmark = pytest.mark.docker

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "buggy_payment_app"
PYTEST_ARGV = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]


@pytest.fixture(scope="module")
def config():
    return SandboxConfig.from_env()


@pytest.fixture(scope="module", autouse=True)
def _docker_ready(config):
    try:
        SandboxManager.check_docker(config)
    except SandboxError as exc:
        pytest.skip(f"Docker not available: {exc}")
    if not SandboxManager.image_exists(config):
        pytest.fail(
            f"Sandbox image {config.image!r} is missing. Run: python -m repodoctor.sandbox build"
        )


@pytest.fixture
def sandbox(config):
    with SandboxManager(config) as sb:
        yield sb


def test_runs_fixture_tests_inside_sandbox(sandbox):
    summary = sandbox.upload_repo(FIXTURE)
    assert summary.file_count > 0
    result = sandbox.exec(PYTEST_ARGV)
    assert result.exit_code == 1 and not result.timed_out
    assert "1 failed, 31 passed" in result.output


def test_image_has_git(sandbox):
    assert sandbox.exec(["git", "--version"]).ok


@pytest.mark.parametrize("code", ["import time; time.sleep(60)", "while True: pass"])
def test_timeout_kills_runaway_process_and_sandbox_survives(sandbox, code):
    result = sandbox.exec(["python", "-c", code], timeout=2)
    assert result.timed_out
    assert result.duration_seconds < 15
    assert sandbox.exec(["python", "-c", "print('alive')"]).output.strip() == "alive"


def test_runs_as_non_root(sandbox):
    assert sandbox.exec(["id", "-u"]).output.strip() == "10001"


def test_no_network(sandbox):
    result = sandbox.exec(
        [
            "python",
            "-c",
            "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)",
        ]
    )
    assert result.exit_code != 0 and "Traceback" in result.output


def test_filesystem_is_read_only_except_workspace_and_tmp(sandbox):
    assert sandbox.exec(["python", "-c", "open('/rootfile', 'w')"]).exit_code != 0
    assert sandbox.exec(
        ["python", "-c", "open('/workspace/ok.txt', 'w').write('1')"]
    ).ok
    assert sandbox.exec(["python", "-c", "open('/tmp/ok.txt', 'w').write('1')"]).ok


def test_host_environment_does_not_leak(sandbox, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    output = sandbox.exec(["printenv"]).output
    assert "AWS_" not in output and "should-not-leak" not in output


def test_upload_excludes_git_directory(sandbox, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("[core]\n")
    (repo / "a.py").write_text("print('a')\n")
    sandbox.upload_repo(repo)
    listing = sandbox.exec(["ls", "-A", "/workspace"]).output.split()
    assert listing == ["a.py"]


def test_output_is_capped_but_keeps_the_tail(config):
    small = dataclasses.replace(config, max_output_bytes=10_000)
    with SandboxManager(small) as sb:
        result = sb.exec(["python", "-c", "print('x' * 5_000_000); print('THE-END')"])
    assert result.truncated
    assert "THE-END" in result.output
    assert len(result.output.encode()) <= 10_000 + len(TRUNCATION_MARKER)


def test_memory_limit_stops_allocation_bomb(sandbox):
    result = sandbox.exec(
        ["python", "-c", "b = b'x' * (900 * 1024 * 1024); print('ALLOCATED')"]
    )
    assert result.exit_code != 0 and "ALLOCATED" not in result.output


def test_destroy_removes_container(config):
    sb = SandboxManager(config)
    sb.start()
    assert sb.container_present()
    sb.destroy()
    assert not sb.container_present()


def test_context_manager_cleans_up_after_error(config):
    sb = SandboxManager(config)
    with pytest.raises(RuntimeError):
        with sb:
            raise RuntimeError("boom")
    assert not sb.container_present()


def test_cleanup_stale_removes_leftover_containers(config):
    sb = SandboxManager(config)
    sb.start()
    assert SandboxManager.cleanup_stale(config) >= 1
    assert not sb.container_present()
    sb.destroy()  # idempotent after external removal


def test_stdin_is_delivered_to_the_process(sandbox):
    result = sandbox.exec(["cat"], stdin=b"hello from stdin")
    assert result.ok and result.output == "hello from stdin"


def test_python_can_run_a_script_from_stdin(sandbox):
    result = sandbox.exec(
        ["python", "-", "arg1"], stdin=b"import sys; print(sys.argv[1])"
    )
    assert result.ok and result.output.strip() == "arg1"
