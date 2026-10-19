"""The Phase 3 tools against the REAL Docker sandbox (git, stdin, read script, pytest).

Run: python -m pytest -m docker tests/integration/test_tools_docker.py -v
"""

from __future__ import annotations

import pytest

from conftest import BUG_LINE, EXPECTED_PATCH, FAILING_TEST, FIXTURE
from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager
from repodoctor.tools import Toolbox

pytestmark = pytest.mark.docker


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
def toolbox(config):
    with SandboxManager(config) as sandbox:
        tb = Toolbox(sandbox)
        loaded = tb.load_repository({"path": str(FIXTURE)})
        assert loaded.ok, (
            loaded.error
        )  # fails loudly if git/safe.directory misbehaves in the container
        yield tb


def test_repository_loads_with_a_git_baseline(toolbox):
    assert toolbox.baseline_commit and len(toolbox.baseline_commit) == 40


def test_list_read_and_search_inside_the_sandbox(toolbox):
    files = toolbox.list_files()
    assert "app/payment.py" in files.files and not any(
        f.startswith(".git") for f in files.files
    )
    read = toolbox.read_file(
        {"path": "app/payment.py", "start_line": BUG_LINE - 2, "end_line": BUG_LINE}
    )
    assert read.ok and "user_id" in read.content
    found = toolbox.search_code({"pattern": "user_id", "path": "app"})
    assert [(m.path, m.line) for m in found.matches] == [("app/payment.py", BUG_LINE)]


def test_read_file_blocks_traversal_and_missing_files(toolbox):
    assert toolbox.read_file({"path": "../etc/passwd"}).error_code == "invalid_path"
    assert toolbox.read_file({"path": "nope.py"}).error_code == "not_found"


def test_run_tests_reports_the_known_failure(toolbox):
    out = toolbox.run_tests()
    assert out.status == "failed" and out.exit_code == 1
    assert (out.counts.failed, out.counts.passed) == (1, 31)
    assert [f.test_id for f in out.failed_tests] == [FAILING_TEST]


def test_full_debug_cycle_in_the_sandbox(toolbox):
    diff = EXPECTED_PATCH.read_text(encoding="utf-8")

    assert toolbox.run_tests().status == "failed"
    checked = toolbox.check_patch({"diff": diff})
    assert checked.ok and checked.files == ["app/payment.py"]

    assert toolbox.apply_patch({"diff": diff}).error_code == "approval_required"
    toolbox.register_approval(checked.sha256)
    applied = toolbox.apply_patch({"diff": diff})
    assert applied.ok, applied.error

    shown = toolbox.git_diff()
    assert shown.files_changed == ["app/payment.py"] and (
        shown.insertions,
        shown.deletions,
    ) == (1, 1)

    after = toolbox.run_tests()
    assert after.status == "passed" and (after.counts.failed, after.counts.passed) == (
        0,
        32,
    )

    assert toolbox.reset_changes().clean
    assert not toolbox.git_diff().has_changes
    assert toolbox.run_tests().status == "failed"


def test_test_timeout_is_reported_as_timeout(toolbox):
    # A slow test is added INSIDE the sandbox only; nothing on the host changes.
    toolbox._backend.exec(
        [
            "python",
            "-c",
            "open('tests/test_slow.py','w').write('import time\\ndef test_slow():\\n    time.sleep(60)\\n')",
        ]
    )
    out = toolbox.run_tests({"timeout_seconds": 3})
    assert out.ok and out.status == "timeout" and out.timed_out
