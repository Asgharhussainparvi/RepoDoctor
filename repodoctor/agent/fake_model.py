"""FakeModel: a deterministic ReasoningEngine for tests, demos and Phase 4.

Investigation is REAL tool use (search + read through the budgeted tools); only the
patches are scripted. This lets us prove the whole loop without AWS.
"""
from __future__ import annotations

import re
from typing import Sequence

from repodoctor.tools.paths import is_protected_path

from .reasoning import AgentTools, InvestigationFailed, PatchGenerationError
from .schemas import Hypothesis, InvestigationContext, PatchContext, PatchProposal

_UNDEFINED_NAME = re.compile(r"name '([A-Za-z_][A-Za-z0-9_]*)' is not defined")


class FakeModel:
    def __init__(
        self,
        patches: Sequence[str | Exception] = (),
        *,
        hypothesis: Hypothesis | None = None,
        investigate_error: Exception | None = None,
    ) -> None:
        self._patches = list(patches)
        self._hypothesis = hypothesis
        self._investigate_error = investigate_error
        self.investigate_calls = 0
        self.patch_contexts: list[PatchContext] = []

    def investigate(self, context: InvestigationContext, tools: AgentTools) -> Hypothesis:
        self.investigate_calls += 1
        if self._investigate_error is not None:
            raise self._investigate_error
        if self._hypothesis is not None:
            return self._hypothesis
        text = " ".join([f.message for f in context.failure.failed_tests] + [context.failure.output_excerpt])
        match = _UNDEFINED_NAME.search(text)
        if not match:
            raise InvestigationFailed("FakeModel can only localise NameError failures.")
        name = match.group(1)
        found = tools.search_code({"pattern": name, "path": ".", "literal": True, "case_sensitive": True})
        if not found.ok:
            raise InvestigationFailed(f"Code search failed: {found.error}")
        hits = [m for m in found.matches if m.path.endswith(".py") and not is_protected_path(m.path)]
        if not hits:
            raise InvestigationFailed(f"No source file mentions {name!r}.")
        hit = hits[0]
        snippet = tools.read_file(
            {"path": hit.path, "start_line": max(1, hit.line - 3), "end_line": hit.line + 3}
        )
        if not snippet.ok:
            raise InvestigationFailed(f"Could not read {hit.path}: {snippet.error}")
        return Hypothesis(
            file=hit.path, line=hit.line, confidence="high",
            description=f"`{name}` is used in {hit.path} but is never defined in that scope.",
            evidence=[hit.text.strip()],
        )

    def propose_patch(self, context: PatchContext, tools: AgentTools) -> PatchProposal:
        self.patch_contexts.append(context)
        index = len(self.patch_contexts) - 1
        if index >= len(self._patches):
            raise PatchGenerationError("FakeModel has no more scripted patches.")
        item = self._patches[index]
        if isinstance(item, Exception):
            raise item
        return PatchProposal(diff=item, summary=f"Scripted patch #{index + 1}")
