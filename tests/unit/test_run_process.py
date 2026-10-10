"""Real subprocesses (the current Python interpreter), no Docker needed."""
import sys

import pytest

from repodoctor.sandbox.manager import TRUNCATION_MARKER, _run_process


def run(code, *, input_bytes=None, timeout=20, max_bytes=10_000):
    return _run_process(
        [sys.executable, "-c", code], input_bytes=input_bytes, timeout=timeout, max_bytes=max_bytes
    )


def test_captures_output_and_exit_code():
    raw = run("import sys; print('hi'); sys.exit(3)")
    assert raw.returncode == 3 and raw.data.decode().strip() == "hi"
    assert not raw.truncated and not raw.timed_out


def test_merges_stderr_into_output():
    raw = run("import sys; sys.stderr.write('oops')")
    assert b"oops" in raw.data


def test_output_exactly_at_cap_is_not_truncated():
    raw = run("import sys; sys.stdout.write('a' * 1000)", max_bytes=1000)
    assert raw.data == b"a" * 1000 and not raw.truncated


def test_large_output_keeps_head_and_tail():
    raw = run("import sys; sys.stdout.write('HEAD' + 'x' * 2_000_000 + 'END')", max_bytes=1000)
    assert raw.truncated
    assert raw.data.startswith(b"HEAD") and raw.data.endswith(b"END")
    assert TRUNCATION_MARKER in raw.data
    assert len(raw.data) <= 1000 + len(TRUNCATION_MARKER)


def test_timeout_kills_process_quickly():
    raw = run("import time; time.sleep(30)", timeout=1)
    assert raw.timed_out and raw.returncode is None and raw.duration < 10


def test_stdin_is_delivered():
    raw = run("import sys; sys.stdout.write(sys.stdin.read().upper())", input_bytes=b"abc")
    assert raw.data == b"ABC"


def test_missing_binary_raises():
    with pytest.raises(OSError):
        _run_process(["definitely-not-a-real-binary-xyz"], input_bytes=None, timeout=5, max_bytes=100)
