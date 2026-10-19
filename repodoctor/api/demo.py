"""Demo engine: real tool-based investigation, scripted patch. For the sample repository ONLY.

Everything it produces is labelled as simulated in the API (EngineInfo.simulated).
"""

from __future__ import annotations

from pathlib import Path

from repodoctor.agent import FakeModel, PatchGenerationError, PatchProposal
from repodoctor.agent.schemas import PatchContext

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_REPO = ROOT / "fixtures" / "buggy_payment_app"
SAMPLE_PATCH = ROOT / "fixtures" / "expected_patches" / "buggy_payment_app.diff"


class DemoEngine(FakeModel):
    """Stateless across sessions (FakeModel counts calls, which breaks when shared).

    REPODOCTOR_DEMO_PATCH=<file> swaps in a different scripted patch, e.g. fixtures/expected_patches/wrong_patch.diff,
    to demonstrate (and manually test) a fix that does NOT pass verification.
    """

    def __init__(self, patch_path: str | Path | None = None) -> None:
        super().__init__()
        self._patch_path = Path(patch_path) if patch_path else SAMPLE_PATCH

    def propose_patch(self, context: PatchContext, tools) -> PatchProposal:  # type: ignore[override]
        if context.attempt != 1:
            raise PatchGenerationError("The demo engine has only one scripted patch.")
        return PatchProposal(
            diff=self._patch_path.read_text(encoding="utf-8"),
            summary="Scripted demo patch (simulated).",
        )


class UnconfiguredEngine:
    """Stands in when no model is configured; every call reports the AI service as unavailable."""

    def investigate(self, context, tools):
        from repodoctor.agent import ModelUnavailableError

        raise ModelUnavailableError("no model configured")

    def propose_patch(self, context, tools):
        from repodoctor.agent import ModelUnavailableError

        raise ModelUnavailableError("no model configured")
