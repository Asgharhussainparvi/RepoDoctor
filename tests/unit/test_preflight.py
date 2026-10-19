import importlib.util
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
sys.modules["preflight"] = preflight
spec.loader.exec_module(preflight)

Check = preflight.Check


@pytest.mark.parametrize(
    ("version", "status"),
    [((3, 12, 10), "ok"), ((3, 11, 4), "warn"), ((3, 10, 1), "warn"), ((3, 10, 0), "fail"), ((3, 9, 18), "fail")],
)
def test_python_version_rules(version, status):
    assert preflight.check_python(version).status == status


def test_packages_required_vs_optional():
    present = {"pydantic", "fastapi", "uvicorn", "multipart"}
    checks = {c.name: c for c in preflight.check_packages(lambda m: object() if m in present else None)}
    assert checks["package fastapi"].status == "ok" and checks["package strands-agents"].status == "warn"
    broken = {c.name: c for c in preflight.check_packages(lambda m: None)}
    assert broken["package fastapi"].status == "fail" and "requirements-dev.txt" in broken["package fastapi"].hint
    assert broken["package boto3"].status == "warn"  # optional packages never fail the preflight


def test_frontend_build(tmp_path):
    assert preflight.check_frontend(tmp_path).status == "warn"
    (tmp_path / "index.html").write_text("<html>")
    assert preflight.check_frontend(tmp_path).status == "ok"


def test_port_free_or_taken():
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        taken = holder.getsockname()[1]
        holder.listen()
        assert preflight.check_port(taken).status == "warn"  # something that is not RepoDoctor
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert preflight.check_port(free).status == "ok"


def test_ai_check_matrix(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # no ~/.aws/credentials
    have = lambda m: object()  # noqa: E731
    assert preflight.check_ai({"REPODOCTOR_ENGINE": "demo"}, have).status == "ok"
    assert "simulated" in preflight.check_ai({"REPODOCTOR_ENGINE": "demo"}, have).detail
    assert preflight.check_ai({}, have).status == "fail"                                  # bedrock, no model id
    assert preflight.check_ai({"BEDROCK_MODEL_ID": "m"}, lambda m: None).status == "fail"  # no strands
    assert preflight.check_ai({"BEDROCK_MODEL_ID": "m"}, have).status == "warn"           # no credentials visible
    assert preflight.check_ai({"BEDROCK_MODEL_ID": "m", "AWS_BEARER_TOKEN_BEDROCK": "x"}, have).status == "ok"


def test_rendering_and_exit_codes():
    ok = [Check("ok", "a", "fine"), Check("warn", "b", "hmm", "do this")]
    assert preflight.exit_code(ok) == 0
    text = preflight.render(ok)
    assert "[ OK ] a: fine" in text and "[WARN] b: hmm" in text and "-> do this" in text
    assert text.splitlines()[-1] == "READY"
    bad = ok + [Check("fail", "c", "broken", "fix it")]
    assert preflight.exit_code(bad) == 1
    assert preflight.render(bad).splitlines()[-1].startswith("NOT READY: 1 problem(s) to fix")


def test_leftover_container_check_degrades_gracefully():
    assert preflight.check_leftover_containers("definitely-not-docker-xyz").status == "warn"


def test_the_full_demo_logic_end_to_end(sandbox_factory):
    """Same code path as `--full`, with the local sandbox double instead of Docker."""
    checks = preflight.run_full_demo(sandbox_factory)
    assert [c.status for c in checks] == ["ok"], checks
    assert "before 1 failed / 31 passed, after 0 failed / 32 passed" in checks[0].detail


def test_the_full_demo_reports_failure_honestly(sandbox_factory, monkeypatch):
    from repodoctor.api.demo import DemoEngine
    wrong = ROOT / "fixtures" / "expected_patches" / "wrong_patch.diff"
    checks = preflight.run_full_demo(sandbox_factory, DemoEngine(wrong))
    assert checks[0].status == "fail" and "verified" not in checks[0].detail.lower().replace("not verified", "")
