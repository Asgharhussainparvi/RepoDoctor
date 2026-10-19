"""Interactive command line: python -m repodoctor.cli <repo> [--engine bedrock|fake]

The patch is never applied without you typing 'y'. There is deliberately no --yes flag.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from repodoctor.agent import (
    BedrockEngine,
    BedrockSettings,
    FakeModel,
    Orchestrator,
    SessionStore,
    State,
)
from repodoctor.agent.schemas import AgentEvent
from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager

_EXIT = {State.SUCCESS: 0, State.NO_FAILURE: 0, State.REJECTED: 3, State.CANCELLED: 3}


def _report(snap, out: Callable[[str], None], engine) -> None:
    r = snap.report
    out("")
    out("=" * 70)
    out(r.headline if r else snap.message)
    out("=" * 70)
    if r is None:
        return
    if r.root_cause:
        where = (
            f"{r.root_cause.file}:{r.root_cause.line}"
            if r.root_cause.line
            else r.root_cause.file
        )
        out(f"Root cause : {where} - {r.root_cause.description}")
    if r.before:
        out(f"Before     : {r.before.failed} failed / {r.before.passed} passed")
    if r.after:
        out(f"After      : {r.after.failed} failed / {r.after.passed} passed")
    if r.files_changed:
        out(f"Files      : {', '.join(r.files_changed)}")
    if r.verification:
        out(
            f"Verified   : {'YES' if r.verification.verified else 'NO'} ({r.verification.reason_code})"
        )
    out(f"Attempts   : {r.patch_attempts}")
    usage = getattr(engine, "usage_for", None)
    if usage:
        u = usage(r.session_id)
        cost = u.cost_usd(engine.settings)
        out(
            f"Model usage: {u.model_calls} calls, {u.input_tokens} in / {u.output_tokens} out tokens"
            + (
                f", about ${cost:.4f}"
                if cost is not None
                else " (set REPODOCTOR_PRICE_* to see a cost estimate)"
            )
        )


def main(
    argv: list[str] | None = None,
    *,
    sandbox_factory=None,
    input_fn=input,
    out=print,
    engine=None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m repodoctor.cli", description=__doc__
    )
    parser.add_argument("repo", help="path to a Python repository with a failing test")
    parser.add_argument("--engine", choices=["bedrock", "fake"], default="bedrock")
    parser.add_argument(
        "--fake-patch",
        help="unified diff file for --engine fake (a scripted patch, no AWS)",
    )
    parser.add_argument(
        "--db",
        default=":memory:",
        help="SQLite file to keep the session (default: not saved)",
    )
    args = parser.parse_args(argv)

    if engine is None:
        if args.engine == "fake":
            if not args.fake_patch:
                out("--engine fake needs --fake-patch FILE")
                return 2
            engine = FakeModel([Path(args.fake_patch).read_text(encoding="utf-8")])
        else:
            try:
                engine = BedrockEngine(BedrockSettings.from_env())
            except ValueError as exc:
                out(f"Configuration problem: {exc}")
                return 2
    if sandbox_factory is None:
        config = SandboxConfig.from_env()
        try:
            SandboxManager.check_docker(config)
            if not SandboxManager.image_exists(config):
                out("Sandbox image missing. Run: python -m repodoctor.sandbox build")
                return 2
        except SandboxError as exc:
            out(str(exc))
            return 2
        sandbox_factory = lambda: SandboxManager(config)  # noqa: E731

    def on_event(event: AgentEvent) -> None:
        if event.kind in ("state", "info", "error"):
            out(f"  [{event.state.value}] {event.message}")

    orch = Orchestrator(
        SessionStore(args.db), engine, sandbox_factory, on_event=on_event
    )
    sid = orch.create_session(args.repo)
    out(f"Debugging {Path(args.repo).resolve()} (session {sid})")
    snap = orch.start(sid)
    while snap.state is State.WAITING_APPROVAL:
        patch = snap.pending_patch
        out("")
        out(f"PROPOSED PATCH (attempt {patch.attempt}): {patch.summary}")
        out(patch.diff.rstrip())
        answer = (
            input_fn("Approve and apply this patch inside the sandbox? [y/N] ")
            .strip()
            .lower()
        )
        snap = (
            orch.approve(sid, patch.sha256)
            if answer in ("y", "yes")
            else orch.reject(sid, patch.sha256)
        )
    _report(snap, out, engine)
    return _EXIT.get(snap.state, 1)


if __name__ == "__main__":
    sys.exit(main())
