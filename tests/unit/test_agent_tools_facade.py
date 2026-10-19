from conftest import BUG_LINE
import pytest

from repodoctor.agent import AgentTools, FakeModel, InvestigationFailed
from repodoctor.agent.schemas import InvestigationContext
from repodoctor.tools import ToolLimits


def test_engine_tool_surface_excludes_dangerous_operations(toolbox):
    tools = AgentTools(toolbox)
    public = {n for n in dir(tools) if not n.startswith("_")}
    assert public == {
        "list_files",
        "read_file",
        "read_file_complete",
        "search_code",
        "run_tests",
        "git_diff",
    }
    for forbidden in (
        "apply_patch",
        "reset_changes",
        "register_approval",
        "check_patch",
        "load_repository",
    ):
        assert not hasattr(tools, forbidden)
    assert not hasattr(tools, "__dict__")  # slots only


def test_facade_calls_are_budgeted(make_toolbox):
    tb = make_toolbox(ToolLimits(max_tool_calls=2))
    tools = AgentTools(tb)
    assert tools.list_files().ok and tools.git_diff().ok
    assert (
        tools.read_file({"path": "app/payment.py"}).error_code == "tool_budget_exceeded"
    )


def failure_context(toolbox):
    failure = toolbox.run_tests(count_against_budget=False)
    return InvestigationContext(session_id="s", failure=failure, files=[])


def test_fake_model_localises_the_bug_using_real_tools(toolbox):
    model = FakeModel()
    hypothesis = model.investigate(failure_context(toolbox), AgentTools(toolbox))
    assert (hypothesis.file, hypothesis.line, hypothesis.confidence) == (
        "app/payment.py",
        BUG_LINE,
        "high",
    )
    assert "user_id" in hypothesis.description
    tools_used = [c.tool for c in toolbox.calls if c.budgeted]
    assert tools_used == [
        "search_code",
        "read_file",
    ]  # investigation really used the tools


def test_fake_model_never_points_at_tests_or_docs(toolbox):
    h = FakeModel().investigate(failure_context(toolbox), AgentTools(toolbox))
    assert not h.file.startswith("tests/") and h.file.endswith(".py")


def test_fake_model_gives_up_when_tools_are_exhausted(make_toolbox):
    tb = make_toolbox(ToolLimits(max_tool_calls=0))
    ctx = failure_context(tb)
    with pytest.raises(InvestigationFailed):
        FakeModel().investigate(ctx, AgentTools(tb))


def test_fake_model_scripted_patches_and_errors(toolbox):
    from repodoctor.agent import PatchGenerationError
    from repodoctor.agent.schemas import Hypothesis, PatchContext

    ctx = PatchContext(
        session_id="s",
        attempt=1,
        max_attempts=3,
        failure=toolbox.run_tests(count_against_budget=False),
        hypothesis=Hypothesis(file="a.py", description="d"),
    )
    model = FakeModel(["diff-1", PatchGenerationError("boom")])
    assert model.propose_patch(ctx, AgentTools(toolbox)).diff == "diff-1"
    with pytest.raises(PatchGenerationError):
        model.propose_patch(ctx, AgentTools(toolbox))
    with pytest.raises(PatchGenerationError, match="no more"):
        model.propose_patch(ctx, AgentTools(toolbox))
    assert len(model.patch_contexts) == 3
