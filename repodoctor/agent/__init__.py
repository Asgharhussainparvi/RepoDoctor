"""RepoDoctor agent: state machine, orchestrator, verifier, reasoning interface."""

from .bedrock_engine import BedrockEngine, BedrockSettings
from .fake_model import FakeModel
from .orchestrator import Orchestrator, OrchestratorError
from .reasoning import (
    AgentTools,
    InvestigationFailed,
    ModelUnavailableError,
    PatchGenerationError,
    ReasoningEngine,
    ReasoningError,
)
from .schemas import (
    AgentConfig,
    DebugReport,
    Hypothesis,
    PatchProposal,
    SessionSnapshot,
)
from .states import TERMINAL_STATES, TRANSITIONS, InvalidTransition, State, ui_state
from .store import SessionStore
from .verifier import VerificationResult, Verifier

__all__ = [
    "AgentConfig",
    "AgentTools",
    "BedrockEngine",
    "BedrockSettings",
    "DebugReport",
    "FakeModel",
    "Hypothesis",
    "InvalidTransition",
    "InvestigationFailed",
    "ModelUnavailableError",
    "Orchestrator",
    "OrchestratorError",
    "PatchGenerationError",
    "PatchProposal",
    "ReasoningEngine",
    "ReasoningError",
    "SessionSnapshot",
    "SessionStore",
    "State",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "VerificationResult",
    "Verifier",
    "ui_state",
]
