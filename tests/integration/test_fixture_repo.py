"""Contract tests for the golden fixture repository (Phase 1).

These run pytest directly on the host against our own *trusted* fixture to pin
down its behaviour. Untrusted repositories must NEVER be run this way; that
path will go through SandboxManager (Phase 2).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "buggy_payment_app"
PATCH = ROOT / "fixtures" / "expected_patches" / "buggy_payment_app.diff"
EXPECTED_FAILING = "tests.test_payment.test_charge_records_receipt_for_customer"


def _copy_fixture(dest: Path) -> Path:
    target = dest / "repo"
    shutil.copytree(
        FIXTURE, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache")
    )
    return target


def _run_pytest(repo: Path) -> dict:
    junit = repo.parent / "junit.xml"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    root = ET.parse(junit).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    cases = suite.findall("testcase")
    failing = [
        f"{c.get('classname')}.{c.get('name')}"
        for c in cases
        if c.find("failure") is not None or c.find("error") is not None
    ]
    return {
        "total": len(cases),
        "failed": len(failing),
        "passed": len(cases) - len(failing),
        "failing": failing,
    }


def test_fixture_fails_exactly_as_expected(tmp_path):
    result = _run_pytest(_copy_fixture(tmp_path))
    assert result["total"] == 32
    assert result["failed"] == 1
    assert result["passed"] == 31
    assert result["failing"] == [EXPECTED_FAILING]


def test_expected_patch_touches_only_payment_py():
    text = PATCH.read_text(encoding="utf-8")
    assert re.findall(r"^\+\+\+ b/(.+)$", text, flags=re.M) == ["app/payment.py"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_expected_patch_fixes_fixture(tmp_path):
    repo = _copy_fixture(tmp_path)
    for args in (["--check"], []):
        proc = subprocess.run(
            ["git", "apply", *args, str(PATCH)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
    result = _run_pytest(repo)
    assert (result["total"], result["failed"], result["passed"]) == (32, 0, 32)
