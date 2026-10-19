"""RepoDoctor preflight: is everything ready for a demo (or a real run) RIGHT NOW?

  python scripts/preflight.py                  # read-only checks, free
  python scripts/preflight.py --full           # also run one complete sample session in the real Docker sandbox
                                               #   (scripted patch, auto-approved by THIS HARNESS; the product never auto-approves)
  python scripts/preflight.py --ping MODEL_ID  # also send ONE tiny Bedrock request (about 20 tokens, billable)

Exit code 0 when nothing FAILED (warnings are fine), 1 otherwise.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str
    hint: str = ""


# ------------------------------------------------------------------------------------------ individual checks
def check_python(version=sys.version_info) -> Check:
    found = f"{version[0]}.{version[1]}.{version[2]}"
    if (version[0], version[1]) < (3, 10) or (version[0], version[1]) == (3, 10) and version[2] < 1:
        return Check(FAIL, "Python", f"{found} is too old (pydantic needs 3.10.1 or newer)", "Install Python 3.12 and recreate the venv.")
    if (version[0], version[1]) < (3, 12):
        return Check(WARN, "Python", f"{found} works, but the project is tested on 3.12", "Consider Python 3.12.")
    return Check(OK, "Python", found)


def check_packages(find: Callable[[str], object] = importlib.util.find_spec) -> list[Check]:
    required = {"pydantic": "pydantic", "fastapi": "fastapi", "uvicorn": "uvicorn", "multipart": "python-multipart"}
    optional = {"strands": "strands-agents", "boto3": "boto3"}
    checks = []
    for module, package in required.items():
        checks.append(Check(OK if find(module) else FAIL, f"package {package}", "installed" if find(module) else "missing",
                            "" if find(module) else "pip install -r requirements-dev.txt"))
    for module, package in optional.items():
        present = bool(find(module))
        checks.append(Check(OK if present else WARN, f"package {package}", "installed" if present else "not installed (needed only for real model runs)",
                            "" if present else "pip install -r requirements-aws.txt"))
    return checks


def check_frontend(dist: Path = ROOT / "frontend" / "dist") -> Check:
    if (dist / "index.html").is_file():
        return Check(OK, "web UI build", str(dist))
    return Check(WARN, "web UI build", "frontend/dist not found (the API will run, but without the web page)",
                 "cd frontend; npm install; npm run build")


def check_port(port: int, host: str = "127.0.0.1") -> Check:
    with socket.socket() as probe:
        probe.settimeout(1)
        try:
            probe.bind((host, port))
            return Check(OK, f"port {port}", "free")
        except OSError:
            pass
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as response:
            body = json.load(response)
        engine = body.get("engine", {})
        return Check(OK, f"port {port}", f"RepoDoctor is already running here (engine: {engine.get('kind')}, status: {body.get('status')})")
    except Exception:  # noqa: BLE001
        return Check(WARN, f"port {port}", "in use by something that is not RepoDoctor",
                     "Stop it, or set REPODOCTOR_PORT to another port.")


def check_ai(env: Mapping[str, str] = os.environ, find: Callable[[str], object] = importlib.util.find_spec) -> Check:
    engine = env.get("REPODOCTOR_ENGINE", "bedrock")
    if engine == "demo":
        return Check(OK, "AI engine", "DEMO mode: the patch is scripted. The UI shows a DEMO MODE (simulated) banner; say so in the demo.")
    model = env.get("BEDROCK_MODEL_ID", "").strip()
    if not model:
        return Check(FAIL, "AI engine", "REPODOCTOR_ENGINE is 'bedrock' but BEDROCK_MODEL_ID is not set",
                     "Set $env:BEDROCK_MODEL_ID to a profile your account can call, or use $env:REPODOCTOR_ENGINE = 'demo'.")
    if not find("strands"):
        return Check(FAIL, "AI engine", f"model {model} configured, but Strands is not installed", "pip install -r requirements-aws.txt")
    has_credentials = bool(env.get("AWS_BEARER_TOKEN_BEDROCK") or env.get("AWS_ACCESS_KEY_ID") or env.get("AWS_PROFILE")
                           or (Path.home() / ".aws" / "credentials").exists())
    if not has_credentials:
        return Check(WARN, "AI engine", f"model {model}, but no AWS credentials or Bedrock API key were found in this shell",
                     "Set AWS_BEARER_TOKEN_BEDROCK or configure AWS credentials.")
    return Check(OK, "AI engine", f"Bedrock model {model} (access is only proven by a real call: use --ping)")


def check_docker() -> list[Check]:
    from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager

    config = SandboxConfig.from_env()
    try:
        SandboxManager.check_docker(config)
    except SandboxError as exc:
        return [Check(FAIL, "Docker", str(exc), "Start Docker Desktop (Linux containers) and re-run.")]
    checks = [Check(OK, "Docker", "running, Linux containers")]
    if SandboxManager.image_exists(config):
        checks.append(Check(OK, "sandbox image", config.image))
    else:
        checks.append(Check(FAIL, "sandbox image", f"{config.image} not found", "python -m repodoctor.sandbox build"))
    return checks


def check_leftover_containers(docker_bin: str = "docker") -> Check:
    try:
        out = subprocess.run([docker_bin, "ps", "-aq", "--filter", "label=repodoctor.managed=true"],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return Check(WARN, "leftover containers", "could not ask Docker")
    ids = out.stdout.split()
    if out.returncode != 0:
        return Check(WARN, "leftover containers", "could not ask Docker")
    if ids:
        return Check(WARN, "leftover containers", f"{len(ids)} RepoDoctor container(s) exist (sessions waiting for approval, or crashed runs)",
                     "python -m repodoctor.sandbox cleanup")
    return Check(OK, "leftover containers", "none")


def run_bedrock_ping(model_id: str) -> Check:
    spec = importlib.util.spec_from_file_location("check_bedrock", ROOT / "scripts" / "check_bedrock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.main(["--quiet-profiles", "--ping", model_id])
    lines = [l.strip() for l in buffer.getvalue().splitlines() if l.strip()]
    tail = next((l for l in reversed(lines) if l.startswith(("OK in", "FAILED", "Hint"))), "no result")
    if code == 0:
        return Check(OK, "Bedrock call", tail)
    hint = next((l for l in lines if l.startswith("Hint")), "")
    return Check(FAIL, "Bedrock call", tail, hint.replace("Hint: ", ""))


def run_full_demo(sandbox_factory: Callable[[], object] | None = None, engine: object | None = None) -> list[Check]:
    """One complete sample session. The scripted patch is auto-approved here, by this harness only."""
    from repodoctor.agent import Orchestrator, SessionStore, State
    from repodoctor.api.demo import SAMPLE_REPO, DemoEngine
    from repodoctor.sandbox import SandboxConfig, SandboxManager

    if sandbox_factory is None:
        config = SandboxConfig.from_env()
        sandbox_factory = lambda: SandboxManager(config)  # noqa: E731
    started = time.monotonic()
    orch = Orchestrator(SessionStore(), engine or DemoEngine(), sandbox_factory)
    sid = orch.create_session(SAMPLE_REPO)
    snap = orch.start(sid)
    if snap.state is not State.WAITING_APPROVAL:
        return [Check(FAIL, "full demo run", f"stopped in {snap.state.value}: {snap.message}")]
    snap = orch.approve(sid, snap.pending_patch.sha256)
    seconds = time.monotonic() - started
    report = snap.report
    if snap.state is State.SUCCESS and report and report.verification and report.verification.verified:
        return [Check(OK, "full demo run", f"verified in {seconds:.0f}s: before {report.before.failed} failed / {report.before.passed} passed, "
                                           f"after {report.after.failed} failed / {report.after.passed} passed")]
    return [Check(FAIL, "full demo run", f"ended in {snap.state.value}: {snap.message}")]


# ------------------------------------------------------------------------------------------ output
_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


def render(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{_MARK[c.status]} {c.name}: {c.detail}")
        if c.hint and c.status != OK:
            lines.append(f"        -> {c.hint}")
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append("READY" if not failed else f"NOT READY: {failed} problem(s) to fix" + (f", {warned} warning(s)" if warned else ""))
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="run one full sample session in the real Docker sandbox")
    parser.add_argument("--ping", metavar="MODEL_ID", help="send one tiny, billable Bedrock request")
    parser.add_argument("--port", type=int, default=int(os.environ.get("REPODOCTOR_PORT", "8000")))
    args = parser.parse_args(argv)
    logging.getLogger("repodoctor").setLevel(logging.ERROR)  # the report is the output; keep warnings out of it

    checks: list[Check] = [check_python(), *check_packages()]
    try:
        checks += check_docker()
    except ImportError as exc:
        checks.append(Check(FAIL, "RepoDoctor package", f"cannot import: {exc}", "pip install -r requirements-dev.txt"))
    checks += [check_leftover_containers(), check_frontend(), check_port(args.port), check_ai()]
    if args.ping:
        checks.append(run_bedrock_ping(args.ping))
    if args.full:
        docker_ok = all(c.status == OK for c in checks if c.name in ("Docker", "sandbox image"))
        checks += run_full_demo() if docker_ok else [Check(FAIL, "full demo run", "skipped: Docker or the sandbox image is not ready")]
    print(render(checks))
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
