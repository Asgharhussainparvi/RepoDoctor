"""BedrockEngine with a REAL Strands Agent driven by a scripted fake model (no AWS, no cost).

This exercises our tool wrappers, structured-output wiring, usage accounting, limits and
error mapping. Only the network call to Bedrock is replaced.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytest.importorskip("strands")
from strands import Agent  # noqa: E402
from strands.models.model import Model  # noqa: E402

from conftest import FIXTURE  # noqa: E402
from repodoctor.agent import (  # noqa: E402
    AgentTools,
    BedrockEngine,
    BedrockSettings,
    InvestigationFailed,
    ModelUnavailableError,
    PatchGenerationError,
    State,
)
from repodoctor.agent.bedrock_engine import build_investigation_tools  # noqa: E402
from repodoctor.agent.schemas import Hypothesis, InvestigationContext, PatchContext  # noqa: E402
from repodoctor.tools import validate_patch  # noqa: E402

SETTINGS = BedrockSettings(
    model_id="fake-model", price_input_per_mtok=0.1, price_output_per_mtok=0.5
)
GOOD_EDIT = {
    "file": "app/payment.py",
    "old_text": "self._gateway.charge(user_id, amount_cents)",
    "new_text": "self._gateway.charge(customer_id, amount_cents)",
    "summary": "Use customer_id.",
}
GOOD_HYPOTHESIS = {
    "file": "app/payment.py",
    "line": 22,
    "confidence": "high",
    "description": "charge() passes user_id, which is undefined.",
    "evidence": ["return self._gateway.charge(user_id, amount_cents)"],
}


# ------------------------------------------------------------------ scripted model
class ScriptedModel(Model):
    """Each turn is a list of ("text", str) / ("tool", name, args) blocks, or an Exception to raise."""

    def __init__(self, turns, *, loop_last=False):
        self.turns, self.loop_last, self.calls, self._n = list(turns), loop_last, [], 0

    def update_config(self, **kwargs):
        pass

    def get_config(self):
        return {"model_id": "scripted"}

    async def structured_output(
        self, output_model, prompt, system_prompt=None, **kwargs
    ):
        raise NotImplementedError
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(
            {
                "tools": [t["name"] for t in (tool_specs or [])],
                "messages": json.dumps(messages, default=str),
            }
        )
        turn = (
            self.turns[0]
            if (self.loop_last and len(self.turns) == 1)
            else self.turns.pop(0)
        )
        if isinstance(turn, BaseException):
            raise turn
        yield {"messageStart": {"role": "assistant"}}
        for block in turn:
            if block[0] == "text":
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": block[1]}}}
            else:
                self._n += 1
                yield {
                    "contentBlockStart": {
                        "start": {
                            "toolUse": {"toolUseId": f"id{self._n}", "name": block[1]}
                        }
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": json.dumps(block[2])}}
                    }
                }
            yield {"contentBlockStop": {}}
        yield {
            "messageStop": {
                "stopReason": "tool_use"
                if any(b[0] == "tool" for b in turn)
                else "end_turn"
            }
        }
        yield {
            "metadata": {
                "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "metrics": {"latencyMs": 5},
            }
        }


def tool(name, **args):
    return [("tool", name, args)]


class Factory:
    """agent_factory for BedrockEngine: tool-using agents are investigators, tool-less ones write patches."""

    def __init__(self, investigate=(), patches=(), *, loop_investigation=False):
        self.investigate, self.patches, self.loop = (
            list(investigate),
            list(patches),
            loop_investigation,
        )
        self.models: list[ScriptedModel] = []

    def __call__(self, *, system_prompt, tools, hooks):
        if tools:
            model = ScriptedModel(self.investigate, loop_last=self.loop)
        else:
            model = ScriptedModel(self.patches.pop(0))
        self.models.append(model)
        return Agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            callback_handler=None,
            hooks=hooks,
        )


INVESTIGATION = [
    tool("search_code", pattern="user_id", path="app"),
    tool("read_file", path="app/payment.py", start_line=18, end_line=24),
    tool("HypothesisOutput", **GOOD_HYPOTHESIS),
]


def contexts(toolbox, hypothesis=None, previous=()):
    failure = toolbox.run_tests(count_against_budget=False)
    inv = InvestigationContext(
        session_id="s1",
        failure=failure,
        files=["app/payment.py", "tests/test_payment.py"],
    )
    patch = PatchContext(
        session_id="s1",
        attempt=1,
        max_attempts=3,
        failure=failure,
        previous_attempts=list(previous),
        hypothesis=hypothesis
        or Hypothesis(file="app/payment.py", line=22, description="d"),
    )
    return inv, patch


# ------------------------------------------------------------------ investigation
def test_investigation_uses_real_tools_and_returns_a_hypothesis(toolbox):
    factory = Factory(INVESTIGATION)
    engine = BedrockEngine(SETTINGS, agent_factory=factory)
    inv, _ = contexts(toolbox)
    hypothesis = engine.investigate(inv, AgentTools(toolbox))
    assert (hypothesis.file, hypothesis.line, hypothesis.confidence) == (
        "app/payment.py",
        22,
        "high",
    )
    assert [c.tool for c in toolbox.calls if c.budgeted] == ["search_code", "read_file"]
    assert factory.models[0].calls[0]["tools"] == [
        "list_files",
        "read_file",
        "search_code",
        "HypothesisOutput",
    ]
    usage = engine.usage_for("s1")
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (1, 300, 60)
    assert usage.cost_usd(SETTINGS) == pytest.approx((300 * 0.1 + 60 * 0.5) / 1e6)


def test_the_prompt_contains_the_failure_and_marks_content_untrusted(toolbox):
    factory = Factory(INVESTIGATION)
    engine = BedrockEngine(SETTINGS, agent_factory=factory)
    inv, _ = contexts(toolbox)
    engine.investigate(inv, AgentTools(toolbox))
    first_call = factory.models[0].calls[0]["messages"]
    assert (
        "test_charge_records_receipt_for_customer" in first_call
        and "NameError" in first_call
    )


def test_paths_are_normalised_and_test_files_are_refused(toolbox):
    inv, _ = contexts(toolbox)
    ok = Factory(
        [tool("HypothesisOutput", **{**GOOD_HYPOTHESIS, "file": "./app//payment.py"})]
    )
    assert (
        BedrockEngine(SETTINGS, agent_factory=ok)
        .investigate(inv, AgentTools(toolbox))
        .file
        == "app/payment.py"
    )
    for bad in ("tests/test_payment.py", "../etc/passwd", "/abs/path.py"):
        factory = Factory(
            [tool("HypothesisOutput", **{**GOOD_HYPOTHESIS, "file": bad})]
        )
        with pytest.raises(InvestigationFailed):
            BedrockEngine(SETTINGS, agent_factory=factory).investigate(
                inv, AgentTools(toolbox)
            )


def test_runaway_tool_loop_is_stopped(toolbox):
    settings = BedrockSettings(model_id="m", max_model_calls_investigate=4)
    factory = Factory([tool("list_files", path=".")], loop_investigation=True)
    engine = BedrockEngine(settings, agent_factory=factory)
    inv, _ = contexts(toolbox)
    with pytest.raises(InvestigationFailed, match="more than 4 calls"):
        engine.investigate(inv, AgentTools(toolbox))
    assert len(factory.models[0].calls) == 4  # the fifth call never reached the model


def test_tool_errors_come_back_as_text_not_exceptions(toolbox):
    tools = {
        t.tool_name: t for t in build_investigation_tools(AgentTools(toolbox), SETTINGS)
    }
    assert "ERROR [invalid_path]" in tools["read_file"]("../etc/passwd")
    assert "ERROR [not_found]" in tools["read_file"]("nope.py")
    assert "(no matches)" in tools["search_code"]("zzz-not-there")
    assert "app/payment.py" in tools["list_files"](".")
    assert "NameError" not in tools["read_file"](
        "app/payment.py", 20, 23
    ) and "user_id" in tools["read_file"]("app/payment.py", 20, 23)


def test_exhausted_budget_tells_the_model_to_finish(make_toolbox):
    from repodoctor.tools import ToolLimits

    tb = make_toolbox(ToolLimits(max_tool_calls=0))
    tools = {
        t.tool_name: t for t in build_investigation_tools(AgentTools(tb), SETTINGS)
    }
    assert "give your structured answer now" in tools["read_file"]("app/payment.py")


def test_tool_results_are_clipped_to_save_tokens(toolbox):
    small = BedrockSettings(model_id="m", max_tool_result_chars=200)
    tools = {
        t.tool_name: t for t in build_investigation_tools(AgentTools(toolbox), small)
    }
    text = tools["read_file"]("app/money.py", 1, 400)
    assert len(text) < 300 and "truncated" in text


# ------------------------------------------------------------------ patching
def test_a_valid_edit_becomes_a_policy_compliant_diff(toolbox):
    factory = Factory(patches=[[tool("EditProposal", **GOOD_EDIT)]])
    engine = BedrockEngine(SETTINGS, agent_factory=factory)
    _, patch_ctx = contexts(toolbox)
    proposal = engine.propose_patch(patch_ctx, AgentTools(toolbox))
    assert proposal.summary == "Use customer_id."
    assert validate_patch(proposal.diff).files == ("app/payment.py",)
    assert toolbox.check_patch(
        {"diff": proposal.diff}
    ).ok  # and it really applies to the repository
    assert factory.models[0].calls[0]["tools"] == [
        "EditProposal"
    ]  # no tools: one call, the model sees the file
    assert (
        "self._gateway.charge(user_id, amount_cents)"
        in factory.models[0].calls[0]["messages"]
    )


def test_a_bad_edit_is_fed_back_and_corrected_in_the_same_attempt(toolbox):
    bad = {**GOOD_EDIT, "old_text": "self._gateway.charge(userid, amount_cents)"}
    factory = Factory(
        patches=[[tool("EditProposal", **bad), tool("EditProposal", **GOOD_EDIT)]]
    )
    engine = BedrockEngine(SETTINGS, agent_factory=factory)
    _, patch_ctx = contexts(toolbox)
    proposal = engine.propose_patch(patch_ctx, AgentTools(toolbox))
    assert validate_patch(proposal.diff).files == ("app/payment.py",)
    assert "old_text was not found" in factory.models[0].calls[1]["messages"]
    assert engine.usage_for("s1").model_calls == 2


def test_giving_up_after_the_allowed_tries(toolbox):
    bad = {**GOOD_EDIT, "old_text": "does not exist anywhere"}
    factory = Factory(
        patches=[[tool("EditProposal", **bad), tool("EditProposal", **bad)]]
    )
    engine = BedrockEngine(SETTINGS, agent_factory=factory)
    _, patch_ctx = contexts(toolbox)
    with pytest.raises(PatchGenerationError, match="could not be applied"):
        engine.propose_patch(patch_ctx, AgentTools(toolbox))


def test_edits_to_tests_are_refused(toolbox):
    edit = {
        **GOOD_EDIT,
        "file": "tests/test_payment.py",
        "old_text": "service.charge",
        "new_text": "service.refund",
    }
    factory = Factory(
        patches=[[tool("EditProposal", **edit), tool("EditProposal", **edit)]]
    )
    _, patch_ctx = contexts(toolbox)
    with pytest.raises(PatchGenerationError, match="Tests and test configuration"):
        BedrockEngine(SETTINGS, agent_factory=factory).propose_patch(
            patch_ctx, AgentTools(toolbox)
        )


def test_earlier_attempt_feedback_reaches_the_model(toolbox):
    from repodoctor.agent.schemas import AttemptFeedback

    factory = Factory(patches=[[tool("EditProposal", **GOOD_EDIT)]])
    _, patch_ctx = contexts(
        toolbox,
        previous=[
            AttemptFeedback(
                attempt=1,
                reason_code="tests_still_failing",
                message="NameError remained",
            )
        ],
    )
    BedrockEngine(SETTINGS, agent_factory=factory).propose_patch(
        patch_ctx, AgentTools(toolbox)
    )
    prompt = factory.models[0].calls[0]["messages"]
    assert (
        "did NOT work" in prompt
        and "tests_still_failing" in prompt
        and "NameError remained" in prompt
    )


def test_unreadable_target_file_is_a_patch_error(toolbox):
    _, patch_ctx = contexts(
        toolbox, hypothesis=Hypothesis(file="app/ghost.py", description="d")
    )
    with pytest.raises(PatchGenerationError, match="Could not read"):
        BedrockEngine(SETTINGS, agent_factory=Factory()).propose_patch(
            patch_ctx, AgentTools(toolbox)
        )


# ------------------------------------------------------------------ cost controls and errors
def test_session_token_budget_stops_further_calls(toolbox):
    tight = BedrockSettings(model_id="m", max_session_tokens=50)
    factory = Factory(INVESTIGATION, patches=[[tool("EditProposal", **GOOD_EDIT)]])
    engine = BedrockEngine(tight, agent_factory=factory)
    inv, patch_ctx = contexts(toolbox)
    engine.investigate(
        inv, AgentTools(toolbox)
    )  # allowed: nothing used yet, ends above the cap
    with pytest.raises(PatchGenerationError, match="Token budget"):
        engine.propose_patch(patch_ctx, AgentTools(toolbox))


def aws_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "ConverseStream")


@pytest.mark.parametrize(
    "code",
    [
        "AccessDeniedException",
        "ThrottlingException",
        "ValidationException",
        "ResourceNotFoundException",
        "ServiceUnavailableException",
    ],
)
def test_aws_failures_become_model_unavailable(toolbox, code):
    engine = BedrockEngine(SETTINGS, agent_factory=Factory([aws_error(code)]))
    inv, _ = contexts(toolbox)
    with pytest.raises(ModelUnavailableError, match=code):
        engine.investigate(inv, AgentTools(toolbox))
    assert (
        engine.usage_for("s1").model_calls == 0
    )  # a rejected call is not billed, so it is not counted


def test_missing_credentials_become_model_unavailable(toolbox):
    from botocore.exceptions import NoCredentialsError

    engine = BedrockEngine(
        SETTINGS, agent_factory=Factory(patches=[[NoCredentialsError()]])
    )
    _, patch_ctx = contexts(toolbox)
    with pytest.raises(ModelUnavailableError):
        engine.propose_patch(patch_ctx, AgentTools(toolbox))


def test_a_model_that_never_answers_in_structure_is_an_investigation_failure(toolbox):
    factory = Factory(
        [[("text", "I think it is somewhere in payment.py")]], loop_investigation=True
    )
    inv, _ = contexts(toolbox)
    with pytest.raises(InvestigationFailed):
        BedrockEngine(SETTINGS, agent_factory=factory).investigate(
            inv, AgentTools(toolbox)
        )


# ------------------------------------------------------------------ settings
def test_settings_from_env():
    s = BedrockSettings.from_env(
        {
            "BEDROCK_MODEL_ID": "global.x",
            "AWS_REGION": "us-west-2",
            "REPODOCTOR_MODEL_MAX_TOKENS": "512",
            "REPODOCTOR_PRICE_INPUT_PER_MTOK": "0.1",
            "REPODOCTOR_PRICE_OUTPUT_PER_MTOK": "0.5",
        }
    )
    assert (s.model_id, s.region, s.max_tokens, s.price_input_per_mtok) == (
        "global.x",
        "us-west-2",
        512,
        0.1,
    )
    assert BedrockSettings.from_env({"BEDROCK_MODEL_ID": "m"}).region == "us-east-1"


def test_settings_errors_are_actionable():
    with pytest.raises(ValueError, match="BEDROCK_MODEL_ID is not set"):
        BedrockSettings.from_env({})
    with pytest.raises(ValueError, match="REPODOCTOR_MODEL_MAX_TOKENS"):
        BedrockSettings.from_env(
            {"BEDROCK_MODEL_ID": "m", "REPODOCTOR_MODEL_MAX_TOKENS": "lots"}
        )
    with pytest.raises(ValueError):
        BedrockSettings(model_id="m", max_tokens=0)


def test_core_package_does_not_import_strands():
    code = "import sys, repodoctor.agent; assert 'strands' not in sys.modules and 'boto3' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=FIXTURE.parent.parent)


# ------------------------------------------------------------------ end to end through the orchestrator
def run_orchestrator(make_orchestrator, factory, **kwargs):
    engine = BedrockEngine(SETTINGS, agent_factory=factory)
    orch = make_orchestrator(engine, **kwargs)
    return orch, engine, orch.create_session(FIXTURE)


def test_full_session_with_a_real_strands_agent(make_orchestrator):
    factory = Factory(INVESTIGATION, patches=[[tool("EditProposal", **GOOD_EDIT)]])
    from repodoctor.agent import SessionStore

    store = SessionStore()
    orch, engine, sid = run_orchestrator(make_orchestrator, factory, store=store)
    waiting = orch.start(sid)
    assert waiting.state is State.WAITING_APPROVAL and waiting.pending_patch.files == [
        "app/payment.py"
    ]
    final = orch.approve(sid, waiting.pending_patch.sha256)
    assert final.state is State.SUCCESS
    assert (final.report.before.failed, final.report.after.passed) == (1, 32)
    assert final.report.root_cause.line == 22
    usage = engine.usage_for(sid)
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (2, 400, 80)
    assert [c["tool"] for c in store.tool_calls(sid) if c["budgeted"]] == [
        "search_code",
        "read_file",
        "read_file_complete",
    ]


def test_a_wrong_model_fix_is_caught_and_retried_with_feedback(make_orchestrator):
    wrong = {**GOOD_EDIT, "new_text": "self._gateway.charge(other_id, amount_cents)"}
    factory = Factory(
        INVESTIGATION,
        patches=[[tool("EditProposal", **wrong)], [tool("EditProposal", **GOOD_EDIT)]],
    )
    orch, engine, sid = run_orchestrator(make_orchestrator, factory)
    second = orch.approve(sid, orch.start(sid).pending_patch.sha256)
    assert second.state is State.WAITING_APPROVAL and second.patch_attempts == 2
    assert (
        "tests_still_failing" in factory.models[2].calls[0]["messages"]
    )  # attempt 1's failure reached the model
    assert orch.approve(sid, second.pending_patch.sha256).state is State.SUCCESS


def test_aws_outage_is_reported_in_plain_words(make_orchestrator):
    orch, _, sid = run_orchestrator(
        make_orchestrator, Factory([aws_error("AccessDeniedException")])
    )
    snap = orch.start(sid)
    assert snap.state is State.FAILED and snap.error_code == "ai_unavailable"
    assert snap.message == "AI service unavailable. Check AWS configuration."


def test_model_blaming_a_test_file_fails_cleanly(make_orchestrator):
    factory = Factory(
        [
            tool(
                "HypothesisOutput",
                **{**GOOD_HYPOTHESIS, "file": "tests/test_payment.py"},
            )
        ]
    )
    orch, _, sid = run_orchestrator(make_orchestrator, factory)
    snap = orch.start(sid)
    assert (
        snap.error_code == "investigation_failed"
        and "does not modify tests" in snap.message
    )


def test_usage_accounting_survives_many_agents_and_garbage_collection(toolbox):
    import gc

    engine = BedrockEngine(
        SETTINGS,
        agent_factory=Factory(
            INVESTIGATION * 1,
            patches=[[tool("EditProposal", **GOOD_EDIT)] for _ in range(1)],
        ),
    )
    inv, patch_ctx = contexts(toolbox)
    engine.investigate(inv, AgentTools(toolbox))
    gc.collect()
    engine.propose_patch(patch_ctx, AgentTools(toolbox))
    usage = engine.usage_for("s1")
    assert (usage.model_calls, usage.input_tokens, usage.output_tokens) == (2, 400, 80)
