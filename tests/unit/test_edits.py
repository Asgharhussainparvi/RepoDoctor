import os
import shutil
import subprocess
from pathlib import Path

import pytest

from repodoctor.tools import (
    EditError,
    ToolLimits,
    apply_edit,
    build_diff,
    make_edit_diff,
    validate_patch,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SRC = "def charge(customer_id, amount):\n    validate(amount)\n    return gateway.charge(user_id, amount)\n\ndef refund():\n    pass\n"


# ----------------------------------------------------------------- apply_edit
def test_exact_unique_edit():
    out = apply_edit(
        SRC, "gateway.charge(user_id, amount)", "gateway.charge(customer_id, amount)"
    )
    assert out == SRC.replace("user_id", "customer_id")


def test_multiline_edit():
    out = apply_edit(
        SRC,
        "    validate(amount)\n    return gateway.charge(user_id, amount)\n",
        "    validate(amount)\n    return gateway.charge(customer_id, amount)\n",
    )
    assert "customer_id, amount)" in out and out.count("validate") == 1


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ("", "x", "empty_old_text"),
        ("   \n", "x", "empty_old_text"),
        ("nope()", "x", "no_match"),
        ("pass", "pass", "no_change"),
        ("amount", "value", "ambiguous_match"),
    ],
)
def test_edit_errors_have_codes(old, new, code):
    with pytest.raises(EditError) as err:
        apply_edit(SRC, old, new)
    assert err.value.code == code and str(err.value)


def test_less_indentation_matches_as_a_substring_and_keeps_the_files_indentation():
    out = apply_edit(
        SRC,
        "  return gateway.charge(user_id, amount)",
        "  return gateway.charge(customer_id, amount)",
    )
    assert "    return gateway.charge(customer_id, amount)" in out  # still 4 spaces


def test_more_indentation_than_the_file_is_refused():
    with pytest.raises(EditError) as err:
        apply_edit(SRC, "      return gateway.charge(user_id, amount)", "x")
    assert err.value.code == "no_match"


def test_wrong_indentation_on_a_multiline_edit_is_refused():
    with pytest.raises(EditError) as err:
        apply_edit(SRC, "validate(amount)\nreturn gateway.charge(user_id, amount)", "x")
    assert err.value.code == "no_match"


def test_trailing_whitespace_differences_are_tolerated():
    content = "a = 1  \nb = 2\n"
    assert apply_edit(content, "a = 1\nb = 2", "a = 1\nb = 3") == "a = 1\nb = 3\n"


def test_crlf_files_keep_crlf():
    content = SRC.replace("\n", "\r\n")
    out = apply_edit(
        content,
        "gateway.charge(user_id, amount)",
        "gateway.charge(customer_id, amount)",
    )
    assert (
        out == content.replace("user_id", "customer_id")
        and "\r\n" in out
        and "\n" not in out.replace("\r\n", "")
    )


def test_crlf_old_text_from_the_model_still_matches():
    content = SRC.replace("\n", "\r\n")
    out = apply_edit(
        content,
        "validate(amount)\r\n    return gateway.charge(user_id, amount)",
        "validate(amount)\n    return gateway.charge(customer_id, amount)",
    )
    assert "customer_id" in out and "\r\n" in out


# ------------------------------------- generated diffs are valid and exact
def git_apply(tmp_path: Path, original: str, diff: str, rel="pkg/mod.py") -> bytes:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(original.encode())
    patch = tmp_path / "x.diff"
    patch.write_bytes(diff.encode())
    # Match the Linux sandbox: ignore the developer's git config (Git for Windows defaults to core.autocrlf=true).
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    proc = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "apply",
            "--recount",
            "--whitespace=nowarn",
            str(patch),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return target.read_bytes()


CASES = {
    "middle": (SRC, "validate(amount)", "validate(amount, strict=True)"),
    "first line": (
        SRC,
        "def charge(customer_id, amount):",
        "def charge(customer_id, amount, *, retries=0):",
    ),
    "last line": (SRC, "    pass\n", "    return None\n"),
    "no trailing newline, edit last line": ("a = 1\nb = 2", "b = 2", "b = 3"),
    "no trailing newline, edit first line": ("a = 1\nb = 2", "a = 1", "a = 9"),
    "single line file": ("x = 1\n", "x = 1", "x = 2"),
    "adds lines": (
        SRC,
        "    validate(amount)\n",
        "    validate(amount)\n    log(amount)\n    audit(amount)\n",
    ),
    "removes lines": (SRC, "    validate(amount)\n", ""),
    "crlf": (SRC.replace("\n", "\r\n"), "validate(amount)", "validate(amount, True)"),
    "blank-line context": ("a = 1\n\n\nb = 2\n\n\nc = 3\n", "b = 2", "b = 20"),
    "tabs": ("def f():\n\treturn 1\n", "\treturn 1", "\treturn 2"),
    "unicode": (
        "name = 'caf\u00e9'\nprint(name)\n",
        "print(name)",
        "print(name.upper())",
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_generated_diff_applies_to_the_exact_expected_bytes(tmp_path, name):
    content, old, new = CASES[name]
    expected = apply_edit(content, old, new)
    diff = make_edit_diff("pkg/mod.py", content, old, new)
    assert git_apply(tmp_path, content, diff) == expected.encode()


@pytest.mark.parametrize("name", list(CASES))
def test_generated_diff_passes_the_patch_policy(name):
    content, old, new = CASES[name]
    patch = validate_patch(
        make_edit_diff("pkg/mod.py", content, old, new), ToolLimits()
    )
    assert patch.files == ("pkg/mod.py",)


def test_no_newline_marker_is_emitted_when_needed():
    diff = make_edit_diff("m.py", "a = 1\nb = 2", "b = 2", "b = 3")
    assert (
        diff.count("\\ No newline at end of file") == 2
    )  # old and new last lines both lack it


def test_unchanged_content_produces_an_empty_diff():
    assert build_diff("m.py", SRC, SRC) == ""


def test_large_file_diff_stays_small():
    big = "".join(f"line_{i} = {i}\n" for i in range(2000))
    diff = make_edit_diff("big.py", big, "line_1000 = 1000", "line_1000 = -1")
    assert len(diff.splitlines()) < 20 and validate_patch(diff).insertions == 1
