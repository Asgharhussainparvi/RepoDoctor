"""REAL Amazon Bedrock calls. Opt-in, cost real (small) money, skipped by default.

Enable (PowerShell):
  $env:REPODOCTOR_RUN_AWS_TESTS = "1"
  $env:BEDROCK_MODEL_ID = "<an inference profile id your account can call>"
  python -m pytest -m aws -v -s
"""

from __future__ import annotations

import os

import pytest

from conftest import FIXTURE

pytestmark = pytest.mark.aws


@pytest.fixture(scope="module", autouse=True)
def _aws_enabled():
    if os.environ.get("REPODOCTOR_RUN_AWS_TESTS") != "1":
        pytest.skip("AWS tests are opt-in: set REPODOCTOR_RUN_AWS_TESTS=1")
    if not os.environ.get("BEDROCK_MODEL_ID"):
        pytest.skip("BEDROCK_MODEL_ID is not set")
    pytest.importorskip("strands")
    pytest.importorskip("boto3")


@pytest.fixture
def engine():
    from repodoctor.agent import BedrockEngine, BedrockSettings

    return BedrockEngine(BedrockSettings.from_env())


def test_real_model_localises_the_bug(engine, toolbox):
    from repodoctor.agent import AgentTools
    from repodoctor.agent.schemas import InvestigationContext

    failure = toolbox.run_tests(count_against_budget=False)
    hypothesis = engine.investigate(
        InvestigationContext(
            session_id="aws1", failure=failure, files=toolbox.list_files().files
        ),
        AgentTools(toolbox),
    )
    print("\nhypothesis:", hypothesis.model_dump())
    print("usage:", engine.usage_for("aws1"))
    assert hypothesis.file == "app/payment.py"


def test_real_model_full_session(engine, make_orchestrator):
    from repodoctor.agent import State

    orch = make_orchestrator(engine)
    sid = orch.create_session(FIXTURE)
    waiting = orch.start(sid)
    print("\nstate:", waiting.state, waiting.message)
    assert waiting.state is State.WAITING_APPROVAL, waiting.message
    print(waiting.pending_patch.diff)
    final = orch.approve(sid, waiting.pending_patch.sha256)
    print("usage:", engine.usage_for(sid), "| final:", final.state, final.message)
    assert final.state in (
        State.SUCCESS,
        State.WAITING_APPROVAL,
    )  # a wrong first fix retries, never fakes success
