"""Toolbox behaviour with real git and real pytest on the trusted fixture (no Docker)."""

import os
from pathlib import Path

import pytest

from conftest import BUG_LINE, FAILING_TEST
from repodoctor.sandbox.errors import DockerUnavailableError, SandboxError
from repodoctor.tools import ToolLimits, patch_sha256, validate_patch

FIXTURE_FILES = [
    "README.md",
    "app/__init__.py",
    "app/gateway.py",
    "app/money.py",
    "app/payment.py",
    "pytest.ini",
    "requirements.txt",
    "tests/test_gateway.py",
    "tests/test_money.py",
    "tests/test_payment.py",
]


# ---------------------------------------------------------------- load / list
def test_load_repository_creates_baseline_commit(toolbox):
    assert toolbox.baseline_commit and len(toolbox.baseline_commit) == 40


def test_tools_refuse_before_a_repository_is_loaded(make_toolbox):
    tb = make_toolbox(load=False)
    for result in (
        tb.list_files(),
        tb.run_tests(),
        tb.git_diff(),
        tb.read_file({"path": "a.py"}),
    ):
        assert not result.ok and result.error_code == "repository_not_loaded"


def test_list_files_returns_repo_files_but_never_git(toolbox):
    out = toolbox.list_files()
    assert out.ok and out.files == FIXTURE_FILES and not out.truncated


def test_list_files_subdirectory_and_missing_path(toolbox):
    assert toolbox.list_files({"path": "app"}).files == [
        f for f in FIXTURE_FILES if f.startswith("app/")
    ]
    assert toolbox.list_files({"path": "nope"}).files == []


def test_list_files_truncates(toolbox):
    out = toolbox.list_files({"max_files": 3})
    assert len(out.files) == 3 and out.truncated


@pytest.mark.parametrize("path", ["../x", "/etc", ".git", "a\\b"])
def test_list_files_rejects_unsafe_paths(toolbox, path):
    out = toolbox.list_files({"path": path})
    assert not out.ok and out.error_code == "invalid_path"


# ----------------------------------------------------------------- read_file
def test_read_file_returns_requested_lines(toolbox):
    first, last = BUG_LINE - 2, BUG_LINE
    out = toolbox.read_file(
        {"path": "app/payment.py", "start_line": first, "end_line": last}
    )
    assert out.ok and out.start_line == first and out.end_line == last
    assert "user_id" in out.content and out.total_lines > last and out.has_more
    assert out.numbered().startswith(f"{first:>5} | ")


def test_read_file_whole_small_file(toolbox):
    out = toolbox.read_file({"path": "tests/test_gateway.py"})
    assert out.ok and not out.has_more and out.content.startswith("from app.gateway")


def test_read_file_caps_lines(make_toolbox):
    tb = make_toolbox(ToolLimits(max_read_lines=5))
    out = tb.read_file({"path": "app/money.py"})
    assert out.end_line == 5 and out.has_more and len(out.content.split("\n")) == 5


def test_read_file_past_end_is_empty_not_an_error(toolbox):
    out = toolbox.read_file({"path": "tests/test_gateway.py", "start_line": 500})
    assert out.ok and out.content == "" and not out.has_more


def test_read_file_errors(toolbox, local_sandbox):
    assert toolbox.read_file({"path": "missing.py"}).error_code == "not_found"
    assert toolbox.read_file({"path": "../x"}).error_code == "invalid_path"
    assert toolbox.read_file({"path": "app"}).error_code == "not_found"  # a directory
    (
        local_sandbox_path := __import__("pathlib").Path(local_sandbox.workspace)
        / "blob.bin"
    ).write_bytes(b"ab\x00cd")
    assert toolbox.read_file({"path": "blob.bin"}).error_code == "binary_file"


def test_read_file_size_limit_and_long_line_cut(make_toolbox, local_sandbox):
    from pathlib import Path

    tb = make_toolbox(ToolLimits(max_file_bytes=200, max_line_chars=10))
    assert tb.read_file({"path": "app/payment.py"}).error_code == "file_too_large"
    (Path(local_sandbox.workspace) / "wide.txt").write_text("x" * 50 + "\n")
    out = tb.read_file({"path": "wide.txt"})
    assert out.content == "x" * 10 and out.long_lines_cut


def test_read_file_does_not_follow_symlink_outside_workspace(
    toolbox, local_sandbox, tmp_path
):
    from pathlib import Path

    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n")
    try:
        os.symlink(secret, Path(local_sandbox.workspace) / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    out = toolbox.read_file({"path": "link.txt"})
    assert (
        not out.ok and out.error_code == "invalid_path" and "secret" not in out.content
    )


# --------------------------------------------------------------- search_code
def test_search_finds_the_bug(toolbox):
    out = toolbox.search_code({"pattern": "user_id"})
    # the fixture README also names the bug, so search the whole repo and the source dir separately
    assert out.ok and {m.path for m in out.matches} == {"README.md", "app/payment.py"}
    scoped = toolbox.search_code({"pattern": "user_id", "path": "app"})
    assert [(m.path, m.line) for m in scoped.matches] == [("app/payment.py", BUG_LINE)]
    assert "gateway.charge(user_id" in scoped.matches[0].text


def test_search_is_literal_by_default_and_regex_on_request(toolbox):
    assert toolbox.search_code(
        {"pattern": "charge(", "path": "app"}
    ).matches  # '(' is not a regex group
    out = toolbox.search_code({"pattern": r"def (charge|refund)\(", "literal": False})
    assert {m.path for m in out.matches} == {"app/payment.py", "app/gateway.py"}


def test_search_case_sensitivity_and_no_match(toolbox):
    assert toolbox.search_code({"pattern": "PAYMENTSERVICE"}).matches
    assert not toolbox.search_code(
        {"pattern": "PAYMENTSERVICE", "case_sensitive": True}
    ).matches
    out = toolbox.search_code({"pattern": "zzz-not-present"})
    assert out.ok and out.matches == []


def test_search_pattern_starting_with_dash_is_safe(toolbox):
    assert toolbox.search_code({"pattern": "--version"}).ok


def test_search_result_cap(toolbox):
    out = toolbox.search_code({"pattern": "def", "max_results": 3})
    assert len(out.matches) == 3 and out.truncated


def test_search_invalid_regex_is_a_clean_error(toolbox):
    out = toolbox.search_code({"pattern": "(", "literal": False})
    assert not out.ok and out.error_code == "command_failed"


# ----------------------------------------------------------------- run_tests
def test_run_tests_reports_the_known_failure(toolbox):
    out = toolbox.run_tests()
    assert out.ok and out.status == "failed" and out.exit_code == 1
    assert (out.counts.failed, out.counts.passed) == (1, 31)
    assert [f.test_id for f in out.failed_tests] == [FAILING_TEST]
    assert "NameError" in out.output_excerpt and out.summary == "1 failed, 31 passed"


def test_run_tests_survives_repo_addopts_that_hide_the_summary(toolbox, local_sandbox):
    from pathlib import Path

    (Path(local_sandbox.workspace) / "pytest.ini").write_text(
        "[pytest]\npythonpath = .\naddopts = -qq\n"
    )
    out = toolbox.run_tests()
    assert out.status == "failed" and out.counts.passed == 31


def test_run_tests_timeout_is_reported(toolbox, local_sandbox):
    from pathlib import Path

    (Path(local_sandbox.workspace) / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(30)\n"
    )
    out = toolbox.run_tests({"timeout_seconds": 2})
    assert out.ok and out.status == "timeout" and out.timed_out
    assert out.summary == "Test execution exceeded the allowed time."


def test_run_tests_never_reports_unparseable_output_as_passed(toolbox, local_sandbox):
    from pathlib import Path

    (Path(local_sandbox.workspace) / "tests" / "test_a.py").write_text(
        "raise SystemExit(0)\n"
    )
    assert toolbox.run_tests().status != "passed"


# ----------------------------------------------- patch / approval / diff / reset
def test_check_patch_validates_and_dry_runs(toolbox, expected_diff):
    out = toolbox.check_patch({"diff": expected_diff})
    assert (
        out.ok
        and out.files == ["app/payment.py"]
        and out.sha256 == validate_patch(expected_diff).sha256
    )
    assert not toolbox.git_diff().has_changes  # a check never modifies anything


def test_check_patch_rejects_policy_violations_and_non_applying_patches(
    toolbox, expected_diff
):
    assert (
        toolbox.check_patch(
            {"diff": expected_diff.replace("app/payment.py", "tests/test_payment.py")}
        ).error_code
        == "protected_path"
    )
    stale = expected_diff.replace(
        "return self._gateway.charge(user_id", "return self._gateway.charge(other"
    )
    assert toolbox.check_patch({"diff": stale}).error_code == "patch_does_not_apply"


def test_apply_requires_approval_of_the_exact_patch(toolbox, expected_diff):
    out = toolbox.apply_patch({"diff": expected_diff})
    assert not out.ok and out.error_code == "approval_required"
    assert not toolbox.git_diff().has_changes

    other = expected_diff.replace(
        "customer_id, amount_cents", "customer_id,  amount_cents"
    )
    toolbox.register_approval(patch_sha256(expected_diff))  # approved A ...
    out = toolbox.apply_patch({"diff": other})  # ... but B is submitted
    assert out.error_code == "approval_required"


def test_register_approval_validates_hash_format(toolbox):
    with pytest.raises(ValueError):
        toolbox.register_approval("not-a-hash")


def test_full_debug_cycle(toolbox, expected_diff):
    before = toolbox.run_tests()
    assert before.status == "failed"

    checked = toolbox.check_patch({"diff": expected_diff})
    toolbox.register_approval(checked.sha256)
    applied = toolbox.apply_patch({"diff": expected_diff})
    assert applied.ok and applied.files_changed == ["app/payment.py"]

    diff = toolbox.git_diff()
    assert diff.has_changes and diff.files_changed == ["app/payment.py"]
    assert (diff.insertions, diff.deletions) == (1, 1)
    assert "-        return self._gateway.charge(user_id, amount_cents)" in diff.diff
    assert (
        "+        return self._gateway.charge(customer_id, amount_cents)" in diff.diff
    )

    after = toolbox.run_tests()
    assert after.status == "passed" and (after.counts.failed, after.counts.passed) == (
        0,
        32,
    )

    reset = toolbox.reset_changes()
    assert reset.ok and reset.clean and not toolbox.git_diff().has_changes
    assert toolbox.run_tests().status == "failed"  # back to the original bug


def test_approval_is_single_use(toolbox, expected_diff):
    toolbox.register_approval(patch_sha256(expected_diff))
    assert toolbox.apply_patch({"diff": expected_diff}).ok
    toolbox.reset_changes()
    assert (
        toolbox.apply_patch({"diff": expected_diff}).error_code == "approval_required"
    )


def test_wrong_patch_does_not_verify(toolbox, expected_diff):
    wrong = expected_diff.replace("customer_id, amount_cents", "other_id, amount_cents")
    toolbox.register_approval(patch_sha256(wrong))
    assert toolbox.apply_patch({"diff": wrong}).ok
    after = toolbox.run_tests()
    assert after.status == "failed"  # the failure is still reported honestly
    assert after.counts.failed == 1


def test_verification_only_proves_the_tests_pass(toolbox, expected_diff):
    """Known limitation, kept visible: a hardcoded value satisfies the suite, so it 'verifies'.

    RepoDoctor reports test evidence, not proof of correctness. The human reviewing
    the diff before approval is what catches this kind of patch.
    """
    hardcoded = expected_diff.replace(
        "customer_id, amount_cents", "'cust_42', amount_cents"
    )
    toolbox.register_approval(patch_sha256(hardcoded))
    assert toolbox.apply_patch({"diff": hardcoded}).ok
    assert toolbox.run_tests().status == "passed"


def test_reset_removes_untracked_files_too(toolbox, local_sandbox):
    from pathlib import Path

    (Path(local_sandbox.workspace) / "junk.txt").write_text("x")
    assert toolbox.reset_changes().clean
    assert "junk.txt" not in toolbox.list_files().files


# -------------------------------------------------- budget / validation / errors
def test_budget_applies_to_agent_tools_only(make_toolbox, expected_diff):
    tb = make_toolbox(ToolLimits(max_tool_calls=2))
    assert tb.list_files().ok and tb.list_files().ok
    out = tb.list_files()
    assert (
        not out.ok
        and out.error_code == "tool_budget_exceeded"
        and tb.budget_remaining == 0
    )
    assert tb.check_patch(
        {"diff": expected_diff}
    ).ok  # orchestrator operations are exempt
    assert tb.reset_changes().ok


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("read_file", {"path": "a.py", "start_line": 0}),
        ("read_file", {"path": "a.py", "start_line": 5, "end_line": 2}),
        ("read_file", {}),
        ("search_code", {"pattern": ""}),
        ("search_code", {"pattern": "a\nb"}),
        ("list_files", {"surprise": 1}),
        ("run_tests", {"timeout_seconds": 0}),
        ("run_tests", {"timeout_seconds": 999}),
        ("git_diff", {"x": 1}),
        ("check_patch", {}),
    ],
)
def test_invalid_input_is_a_structured_error(toolbox, method, payload):
    out = getattr(toolbox, method)(payload)
    assert not out.ok and out.error_code == "invalid_input" and out.error


def test_non_object_input_rejected(toolbox):
    assert toolbox.list_files("app").error_code == "invalid_input"


def test_call_records_and_timing(toolbox):
    toolbox.list_files()
    toolbox.read_file({"path": "missing.py"})
    names = [(c.tool, c.ok, c.error_code) for c in toolbox.calls]
    assert names[-2:] == [("list_files", True, None), ("read_file", False, "not_found")]
    assert all(c.duration_seconds >= 0 for c in toolbox.calls)


def test_sandbox_errors_are_mapped_not_raised(toolbox, monkeypatch):
    def boom(*a, **k):
        raise DockerUnavailableError("Sandbox unavailable. Docker must be running.")

    monkeypatch.setattr(toolbox._backend, "exec", boom)
    out = toolbox.list_files()
    assert (
        not out.ok
        and out.error_code == "sandbox_unavailable"
        and "Docker must be running" in out.error
    )

    def generic(*a, **k):
        raise SandboxError("container exploded")

    monkeypatch.setattr(toolbox._backend, "exec", generic)
    assert toolbox.run_tests().error_code == "sandbox_error"


def test_unexpected_exceptions_never_escape(toolbox, monkeypatch):
    def bug(*a, **k):
        raise RuntimeError("bug in handler")

    monkeypatch.setattr(toolbox._backend, "exec", bug)
    out = toolbox.run_tests()
    assert not out.ok and out.error_code == "internal_error" and out.status == "error"


def test_failed_tool_outputs_never_look_like_a_pass(toolbox, monkeypatch):
    monkeypatch.setattr(
        toolbox._backend,
        "exec",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    assert toolbox.run_tests().status == "error"


# ------------------------------------------------- whole-file reads (used to build edit patches)
def test_read_file_complete_returns_the_exact_file(toolbox, local_sandbox):
    out = toolbox.read_file_complete({"path": "app/payment.py"})
    on_disk = (
        (Path(local_sandbox.workspace) / "app" / "payment.py").read_bytes().decode()
    )
    assert out.ok and out.content + ("\n" if out.ends_with_newline else "") == on_disk
    assert out.start_line == 1 and out.end_line == out.total_lines and not out.has_more


def test_read_file_complete_preserves_crlf_and_missing_final_newline(
    toolbox, local_sandbox
):
    (Path(local_sandbox.workspace) / "crlf.txt").write_bytes(b"a\r\nb\r\nc")
    out = toolbox.read_file_complete({"path": "crlf.txt"})
    assert out.content == "a\r\nb\r\nc" and not out.ends_with_newline
    (Path(local_sandbox.workspace) / "empty.txt").write_bytes(b"")
    empty = toolbox.read_file_complete({"path": "empty.txt"})
    assert empty.ok and empty.content == "" and not empty.ends_with_newline


def test_read_file_complete_is_not_truncated_by_long_lines(make_toolbox, local_sandbox):
    tb = make_toolbox(ToolLimits(max_line_chars=10))
    (Path(local_sandbox.workspace) / "wide.txt").write_bytes(
        b"x" * 500 + b"\n"
    )  # bytes: no Windows newline translation
    assert tb.read_file_complete({"path": "wide.txt"}).content == "x" * 500


def test_read_file_complete_refuses_huge_files(make_toolbox, local_sandbox):
    tb = make_toolbox(ToolLimits(max_edit_file_lines=5))
    (Path(local_sandbox.workspace) / "long.txt").write_text("l\n" * 50)
    out = tb.read_file_complete({"path": "long.txt"})
    assert not out.ok and out.error_code == "file_too_large"


def test_read_file_complete_counts_as_exactly_one_budgeted_call(make_toolbox):
    tb = make_toolbox(ToolLimits(max_tool_calls=1))
    assert tb.read_file_complete({"path": "app/payment.py"}).ok
    assert tb.budget_remaining == 0
    assert (
        tb.read_file_complete({"path": "app/payment.py"}).error_code
        == "tool_budget_exceeded"
    )


def test_read_file_complete_validates_paths(toolbox):
    assert toolbox.read_file_complete({"path": "../x"}).error_code == "invalid_path"
    assert toolbox.read_file_complete({}).error_code == "invalid_input"
