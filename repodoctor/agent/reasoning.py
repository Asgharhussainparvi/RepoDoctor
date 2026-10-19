"""The ReasoningEngine interface and the restricted tool facade engines receive.

Engines (FakeModel now, Bedrock/Strands later) only PROPOSE: a hypothesis and a
patch. They never change session state, never see apply_patch, reset_changes or the
approval registry, and cannot declare success.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from repodoctor.tools import Toolbox
from repodoctor.tools.models import (
    GitDiffOutput,
    ListFilesOutput,
    ReadFileOutput,
    RunTestsOutput,
    SearchCodeOutput,
)

from .schemas import Hypothesis, InvestigationContext, PatchContext, PatchProposal


class ReasoningError(Exception):
    """Base class for engine failures."""


class ModelUnavailableError(ReasoningError):
    """The AI service cannot be reached (credentials, network, throttling...)."""


class InvestigationFailed(ReasoningError):
    """The engine could not localise the root cause."""


class PatchGenerationError(ReasoningError):
    """The engine produced no usable patch for this attempt (consumes an attempt)."""


class AgentTools:
    """The ONLY tool surface an engine gets: six read/inspect tools, all budgeted.

    This keeps apply/reset/approval out of the model's reach by construction. It is a
    structural guard against accidental exposure, not a defence against hostile
    in-process code; the real approval boundary is the patch-hash gate in the Toolbox.
    """

    __slots__ = ("__toolbox",)

    def __init__(self, toolbox: Toolbox) -> None:
        object.__setattr__(self, "_AgentTools__toolbox", toolbox)

    def list_files(self, inp: Mapping[str, Any] | None = None) -> ListFilesOutput:
        return self.__toolbox.list_files(inp)

    def read_file(self, inp: Mapping[str, Any] | None = None) -> ReadFileOutput:
        return self.__toolbox.read_file(inp)

    def read_file_complete(
        self, inp: Mapping[str, Any] | None = None
    ) -> ReadFileOutput:
        """Whole file, for building edit patches. Counts as one budgeted call."""
        return self.__toolbox.read_file_complete(inp)

    def search_code(self, inp: Mapping[str, Any] | None = None) -> SearchCodeOutput:
        return self.__toolbox.search_code(inp)

    def run_tests(self, inp: Mapping[str, Any] | None = None) -> RunTestsOutput:
        return self.__toolbox.run_tests(inp)

    def git_diff(self, inp: Mapping[str, Any] | None = None) -> GitDiffOutput:
        return self.__toolbox.git_diff(inp)


class ReasoningEngine(Protocol):
    def investigate(
        self, context: InvestigationContext, tools: AgentTools
    ) -> Hypothesis: ...

    def propose_patch(
        self, context: PatchContext, tools: AgentTools
    ) -> PatchProposal: ...
