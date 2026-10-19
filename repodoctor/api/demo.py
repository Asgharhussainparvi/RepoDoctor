"""Demo engine: real tool-based investigation, scripted patch. For the sample repository ONLY.

Everything it produces is labelled as simulated in the API (EngineInfo.simulated).
"""
from __future__ import annotations

from pathlib import Path

from repodoctor.agent import FakeModel, PatchGenerationError, PatchProposal
from repodoctor.agent.schemas import PatchContext
from repodoctor.tools.edits import EditError, make_edit_diff

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_REPO = ROOT / "fixtures" / "buggy_payment_app"
SAMPLE_PATCH = ROOT / "fixtures" / "expected_patches" / "buggy_payment_app.diff"


class DemoEngine(FakeModel):
    """Stateless across sessions (FakeModel counts calls, which breaks when shared).

    The scripted fix is an EDIT (`user_id` -> `customer_id`) applied to the real file, so the diff's line numbers are
    always correct even if an editor reformatted the sample. REPODOCTOR_DEMO_PATCH=<diff file> uses that file as
    written instead, e.g. fixtures/expected_patches/wrong_patch.diff, to demonstrate a fix that does NOT pass verification.
    """

    OLD, NEW = "charge(user_id, amount_cents)", "charge(customer_id, amount_cents)"

    def __init__(self, patch_path: str | Path | None = None) -> None:
        super().__init__()
        self._patch_path = Path(patch_path) if patch_path else None

    def propose_patch(self, context: PatchContext, tools) -> PatchProposal:  # type: ignore[override]
        if context.attempt != 1:
            raise PatchGenerationError("The demo engine has only one scripted patch.")
        summary = "Scripted demo patch (simulated)."
        if self._patch_path is not None:
            return PatchProposal(diff=self._patch_path.read_text(encoding="utf-8"), summary=summary)
        path = context.hypothesis.file
        read = tools.read_file_complete({"path": path})
        if not read.ok:
            raise PatchGenerationError(f"Could not read {path}: {read.error}")
        content = read.content + ("\n" if read.ends_with_newline and read.content else "")
        try:
            return PatchProposal(diff=make_edit_diff(path, content, self.OLD, self.NEW), summary=summary)
        except EditError as exc:
            raise PatchGenerationError(str(exc)) from exc


class UnconfiguredEngine:
    """Stands in when no model is configured; every call reports the AI service as unavailable."""

    def investigate(self, context, tools):
        from repodoctor.agent import ModelUnavailableError
        raise ModelUnavailableError("no model configured")

    def propose_patch(self, context, tools):
        from repodoctor.agent import ModelUnavailableError
        raise ModelUnavailableError("no model configured")
