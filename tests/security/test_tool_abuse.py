"""Phase 9: hostile tool arguments (what a prompt-injected model could ask the read-only tools to do)."""
import os
from pathlib import Path

import pytest

from repodoctor.tools import ToolLimits

SENTINEL = "TOP-SECRET-SENTINEL-4711"
ALLOWED_ERRORS = {"invalid_path", "invalid_input", "not_found"}


@pytest.fixture
def canary(tmp_path):
    return tmp_path / "CANARY-must-never-exist"


def hostile_paths(canary):
    return [
        "../../etc/passwd", "/etc/passwd", "C:\\Windows\\win.ini", ".git/config", ".GIT/config", "app/../../x",
        "a\x00b", "-rf", "--version", "x" * 5000, "app/payment.py\n.git/config",
        f"$(touch {canary})", f";touch {canary}", f"`touch {canary}`", f"app && touch {canary}", f"| touch {canary}",
    ]


def no_leak(output) -> None:
    text = repr(output.model_dump())
    assert SENTINEL not in text and "root:x:0:0" not in text


@pytest.mark.parametrize("tool", ["read_file", "read_file_complete"])
def test_read_tools_refuse_every_hostile_path_without_executing_anything(tool, toolbox, canary):
    for path in hostile_paths(canary):
        out = getattr(toolbox, tool)({"path": path})
        assert not out.ok and out.error_code in ALLOWED_ERRORS, (path[:40], out.error_code)
        no_leak(out)
    assert not canary.exists()


def test_list_and_search_never_leave_the_repository_or_run_a_shell(toolbox, canary):
    for path in hostile_paths(canary):
        listed = toolbox.list_files({"path": path})
        assert (not listed.ok and listed.error_code in ALLOWED_ERRORS) or listed.files == [], path[:40]
        searched = toolbox.search_code({"pattern": "user_id", "path": path})
        assert (not searched.ok and searched.error_code in ALLOWED_ERRORS) or searched.matches == [], path[:40]
    assert not canary.exists()


@pytest.mark.parametrize("literal", [True, False])
def test_hostile_search_patterns_are_just_text(literal, toolbox, canary):
    patterns = ["--help", "-e", "-f /etc/passwd", f"$(touch {canary})", f"`touch {canary}`", f"; touch {canary}",
                "a" * 190, "\\", "[", "(", "*", "\u0000"]
    for pattern in patterns:
        out = toolbox.search_code({"pattern": pattern, "literal": literal})
        assert out.ok or out.error_code in {"invalid_input", "command_failed"}, (pattern[:30], out.error_code)
        no_leak(out)
    assert not canary.exists()


def test_a_catastrophic_regex_cannot_hang_the_tool(make_toolbox, local_sandbox):
    import time
    tb = make_toolbox(ToolLimits(tool_timeout_seconds=3))
    (Path(local_sandbox.workspace) / "evil.txt").write_bytes(b"a" * 5000 + b"!")
    started = time.monotonic()
    out = tb.search_code({"pattern": "(a*)*b", "literal": False})
    assert time.monotonic() - started < 15
    assert out.ok or out.error_code in {"timeout", "command_failed"}


@pytest.mark.parametrize("bad", [{"start_line": 0}, {"start_line": -5}, {"start_line": "1; rm -rf /"}, {"end_line": 10**12},
                                 {"start_line": 10**12}, {"path": 123}, {"extra": 1}])
def test_malformed_arguments_are_validation_errors_not_crashes(bad, toolbox):
    args = {"path": "app/payment.py", **bad}
    out = toolbox.read_file(args)
    assert out.error_code in {"invalid_input"} or out.ok  # huge ranges are clamped, nonsense is rejected
    if out.ok:
        assert len(out.content) < 1_000_000


def test_a_symlink_to_a_secret_outside_the_repository_cannot_be_read(toolbox, local_sandbox, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text(f"{SENTINEL}\n")
    workspace = Path(local_sandbox.workspace)
    try:
        os.symlink(secret, workspace / "link.txt")
        os.symlink(tmp_path, workspace / "linkdir")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    for path in ("link.txt", "linkdir/secret.txt"):
        for tool in ("read_file", "read_file_complete"):
            out = getattr(toolbox, tool)({"path": path})
            assert not out.ok and out.error_code == "invalid_path", (tool, path)
            no_leak(out)
    found = toolbox.search_code({"pattern": SENTINEL})
    assert SENTINEL not in repr(found.model_dump())


def test_the_model_facing_wrappers_return_text_for_every_hostile_argument(toolbox, canary):
    pytest.importorskip("strands")
    from repodoctor.agent import AgentTools, BedrockSettings
    from repodoctor.agent.bedrock_engine import build_investigation_tools
    tools = {t.tool_name: t for t in build_investigation_tools(AgentTools(toolbox), BedrockSettings(model_id="m"))}
    for path in hostile_paths(canary):
        for call in (lambda: tools["read_file"](path), lambda: tools["list_files"](path), lambda: tools["search_code"]("user_id", path)):
            result = call()
            assert isinstance(result, str) and len(result) < 20_000
            assert SENTINEL not in result
    assert not canary.exists()
