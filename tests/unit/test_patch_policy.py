import pytest

from repodoctor.tools import PatchRejected, ToolLimits, normalize_patch, patch_sha256, validate_patch

GOOD = """\
--- a/app/payment.py
+++ b/app/payment.py
@@ -19,7 +19,7 @@
         \"\"\"Charge a customer.\"\"\"
         _validate_customer_id(customer_id)
         validate_amount_cents(amount_cents)
-        return self._gateway.charge(user_id, amount_cents)
+        return self._gateway.charge(customer_id, amount_cents)
 
     def refund(self, customer_id: str, amount_cents: int) -> GatewayReceipt:
         \"\"\"Refund a customer.\"\"\"
"""


def rejected(diff, **limits):
    with pytest.raises(PatchRejected) as info:
        validate_patch(diff, ToolLimits(**limits))
    return info.value.code


def test_good_patch_passes_and_is_summarised():
    p = validate_patch(GOOD)
    assert p.files == ("app/payment.py",) and (p.insertions, p.deletions) == (1, 1)
    assert p.sha256 == patch_sha256(p.diff) and p.diff.endswith("\n")


def test_expected_fixture_patch_passes(expected_diff):
    assert validate_patch(expected_diff).files == ("app/payment.py",)


def test_normalisation_is_idempotent_and_hash_stable():
    fenced = "```diff\n" + GOOD + "```\n\n"
    once = normalize_patch(fenced)
    assert normalize_patch(once) == once
    assert validate_patch(fenced).sha256 == validate_patch(GOOD.rstrip("\n")).sha256


def test_git_style_headers_accepted():
    git_style = "diff --git a/app/payment.py b/app/payment.py\nindex 83db48f..bf2d0a1 100644\n" + GOOD
    assert validate_patch(git_style).files == ("app/payment.py",)


@pytest.mark.parametrize("path", ["tests/test_payment.py", "conftest.py", "pytest.ini", "pyproject.toml",
                                  "pkg/test_x.py"])
def test_test_and_config_files_are_protected(path):
    diff = GOOD.replace("app/payment.py", path)
    assert rejected(diff) == "protected_path"


def test_test_edits_can_be_explicitly_allowed():
    diff = GOOD.replace("app/payment.py", "tests/test_payment.py")
    assert validate_patch(diff, ToolLimits(allow_test_edits=True)).files == ("tests/test_payment.py",)


@pytest.mark.parametrize("bad_path", ["../outside.py", "a/../../etc/passwd", ".git/config", "/abs.py"])
def test_unsafe_paths_rejected(bad_path):
    diff = GOOD.replace("a/app/payment.py", f"a/{bad_path}").replace("b/app/payment.py", f"b/{bad_path}")
    assert rejected(diff) == "invalid_path"


def test_missing_prefixes_rejected():
    assert rejected(GOOD.replace("a/app", "app").replace("b/app", "app")) == "invalid_path"


def test_creation_deletion_rename_mode_binary_rejected():
    assert rejected(GOOD.replace("--- a/app/payment.py", "--- /dev/null")) == "file_creation_not_allowed"
    assert rejected(GOOD.replace("+++ b/app/payment.py", "+++ /dev/null")) == "file_deletion_not_allowed"
    assert rejected(GOOD.replace("+++ b/app/payment.py", "+++ b/app/other.py")) == "rename_not_allowed"
    assert rejected("diff --git a/x b/x\nnew file mode 100644\n" + GOOD) == "file_creation_not_allowed"
    assert rejected("diff --git a/x b/x\ndeleted file mode 100644\n" + GOOD) == "file_deletion_not_allowed"
    assert rejected("diff --git a/x b/y\nrename from x\nrename to y\n" + GOOD) == "rename_not_allowed"
    assert rejected("diff --git a/x b/x\nold mode 100644\nnew mode 100755\n" + GOOD) == "mode_change_not_allowed"
    assert rejected("diff --git a/x b/x\nGIT binary patch\nliteral 3\n") == "binary_patch_not_allowed"


@pytest.mark.parametrize("text", ["", "   \n\n", "just some prose\n", "--- a/app/payment.py\n+++ b/app/payment.py\n"])
def test_empty_or_malformed_rejected(text):
    assert rejected(text) in {"empty_patch", "malformed_patch"}


def test_prose_inside_hunk_rejected():
    assert rejected(GOOD + "Hope this helps!\n") == "malformed_patch"


def test_size_file_count_and_line_limits():
    assert rejected(GOOD, max_patch_bytes=50) == "patch_too_large"
    assert rejected(GOOD, max_patch_changed_lines=1) == "patch_too_large"
    two = GOOD + GOOD.replace("payment.py", "gateway.py")
    assert rejected(two, max_patch_files=1) == "too_many_files"
    assert validate_patch(two, ToolLimits(max_patch_files=2)).files == ("app/payment.py", "app/gateway.py")


def test_nul_bytes_rejected():
    assert rejected(GOOD + "\x00") == "malformed_patch"


def test_removed_dashes_line_inside_hunk_is_not_a_header():
    # A deleted line "-- sql comment" looks like '--- sql comment' once prefixed.
    diff = GOOD.replace("-        return self._gateway.charge(user_id, amount_cents)\n",
                        "-        return self._gateway.charge(user_id, amount_cents)\n--- sql comment\n")
    p = validate_patch(diff)
    assert p.files == ("app/payment.py",) and p.deletions == 2


# ---- defence in depth: git's own header must agree with the ---/+++ lines ----
def _with_git_header(old, new, body=GOOD):
    return f"diff --git a/{old} b/{new}\n" + body


def test_consistent_git_header_is_accepted():
    assert validate_patch(_with_git_header("app/payment.py", "app/payment.py")).files == ("app/payment.py",)


@pytest.mark.parametrize(
    "diff",
    [
        _with_git_header("tests/test_payment.py", "tests/test_payment.py"),      # header names a test file
        _with_git_header("app/payment.py", "tests/test_payment.py"),             # header says old != new
        _with_git_header("app/other.py", "app/other.py"),                         # header names a different file
        'diff --git "a/app/payment.py" "b/app/payment.py"\n' + GOOD,             # quoted form is unsupported
        "diff --git a/app/payment.py\n" + GOOD,                                  # malformed
    ],
)
def test_inconsistent_or_unsupported_git_headers_are_refused(diff):
    assert rejected(diff) in {"malformed_patch", "protected_path", "rename_not_allowed"}


def test_git_header_for_one_file_then_a_protected_second_file_is_refused():
    two = _with_git_header("app/payment.py", "app/payment.py") + _with_git_header(
        "tests/test_payment.py", "tests/test_payment.py", GOOD.replace("app/payment.py", "tests/test_payment.py"))
    assert rejected(two) == "protected_path"


@pytest.mark.parametrize("path", ["pytest.toml", ".pytest.ini", ".pytest.toml", "sitecustomize.py", "usercustomize.py",
                                  "pkg/sitecustomize.py", "evil.pth", "testing/helpers.py"])
def test_more_test_config_and_interpreter_startup_files_are_protected(path):
    assert rejected(GOOD.replace("app/payment.py", path)) == "protected_path"
