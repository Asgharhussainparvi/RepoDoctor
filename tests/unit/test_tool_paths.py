import pytest

from repodoctor.tools.paths import (
    InvalidPathError,
    is_protected_path,
    validate_repo_path,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("app/payment.py", "app/payment.py"),
        ("./app//payment.py", "app/payment.py"),
        ("a/./b", "a/b"),
        ("file.txt", "file.txt"),
    ],
)
def test_valid_paths_are_normalised(raw, expected):
    assert validate_repo_path(raw) == expected


@pytest.mark.parametrize("raw", ["", ".", "./", "/"])
def test_root_only_allowed_when_requested(raw):
    if raw == "/":
        with pytest.raises(InvalidPathError):
            validate_repo_path(raw, allow_root=True)
        return
    with pytest.raises(InvalidPathError):
        validate_repo_path(raw)
    assert validate_repo_path(raw, allow_root=True) == "."


@pytest.mark.parametrize(
    "raw",
    [
        "/etc/passwd",
        "../secret",
        "a/../../b",
        "a/..",
        "C:/Windows",
        "c:\\x",
        "a\\b",
        ".git/config",
        "a/.GIT/hooks",
        " lead",
        "trail ",
        "a\x00b",
        "a\nb",
        "-rf",
        "x" * 301,
        5,
        None,
    ],
)
def test_unsafe_paths_rejected(raw):
    with pytest.raises(InvalidPathError):
        validate_repo_path(raw)


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_x.py",
        "pkg/tests/helper.py",
        "test/foo.py",
        "test_payment.py",
        "pkg/test_a.py",
        "pkg/a_test.py",
        "conftest.py",
        "pkg/conftest.py",
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
        "Tests/Helper.py",
    ],
)
def test_protected_paths(path):
    assert is_protected_path(path)


@pytest.mark.parametrize(
    "path", ["app/payment.py", "src/testing_utils.py", "app/contest.py", "latest/x.py"]
)
def test_normal_source_paths_not_protected(path):
    assert not is_protected_path(path)
