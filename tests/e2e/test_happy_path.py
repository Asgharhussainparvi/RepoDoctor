"""End-to-end happy path with the FakeModel: the demo story, deterministically, without Docker."""
from conftest import FIXTURE
from repodoctor.agent import FakeModel, State


def test_demo_story(make_orchestrator, expected_diff):
    orch = make_orchestrator(FakeModel([expected_diff]))
    sid = orch.create_session(FIXTURE)

    waiting = orch.start(sid)
    assert waiting.state is State.WAITING_APPROVAL
    assert "- " in waiting.pending_patch.diff and "user_id" in waiting.pending_patch.diff

    final = orch.approve(sid, waiting.pending_patch.sha256)
    report = final.report
    assert final.state is State.SUCCESS
    assert report.headline == "DEBUGGING COMPLETE: fix verified by re-running the tests."
    assert (report.before.failed, report.before.passed) == (1, 31)
    assert (report.after.failed, report.after.passed) == (0, 32)
    assert report.verification.verified and report.files_changed == ["app/payment.py"]
