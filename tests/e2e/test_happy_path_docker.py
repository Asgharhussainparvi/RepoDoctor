"""The same happy path against the REAL Docker sandbox (still no AWS).

Run: python -m pytest -m docker tests/e2e -v
"""
from __future__ import annotations

import pytest

from conftest import FIXTURE
from repodoctor.agent import AgentConfig, FakeModel, Orchestrator, SessionStore, State
from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def config():
    return SandboxConfig.from_env()


@pytest.fixture(scope="module", autouse=True)
def _docker_ready(config):
    try:
        SandboxManager.check_docker(config)
    except SandboxError as exc:
        pytest.skip(f"Docker not available: {exc}")
    if not SandboxManager.image_exists(config):
        pytest.fail(f"Sandbox image {config.image!r} is missing. Run: python -m repodoctor.sandbox build")


class Managers:
    def __init__(self, config):
        self.config, self.created = config, []

    def __call__(self):
        manager = SandboxManager(self.config)
        self.created.append(manager)
        return manager


def orchestrator(config, patches, tmp_path):
    managers = Managers(config)
    return Orchestrator(SessionStore(tmp_path / "s.sqlite3"), FakeModel(patches), managers), managers


def test_happy_path_in_a_real_container(config, tmp_path, expected_diff):
    orch, managers = orchestrator(config, [expected_diff], tmp_path)
    sid = orch.create_session(FIXTURE)

    waiting = orch.start(sid)
    assert waiting.state is State.WAITING_APPROVAL
    assert managers.created[0].container_present()  # alive while we wait for the human

    final = orch.approve(sid, waiting.pending_patch.sha256)
    assert final.state is State.SUCCESS
    assert (final.report.before.failed, final.report.before.passed) == (1, 31)
    assert (final.report.after.failed, final.report.after.passed) == (0, 32)
    assert final.report.verification.authentic()
    assert not managers.created[0].container_present()  # destroyed after the session


def test_reject_in_a_real_container_cleans_up(config, tmp_path, expected_diff):
    orch, managers = orchestrator(config, [expected_diff], tmp_path)
    sid = orch.create_session(FIXTURE)
    waiting = orch.start(sid)
    final = orch.reject(sid, waiting.pending_patch.sha256)
    assert final.state is State.REJECTED and final.report.verification is None
    assert not managers.created[0].container_present()


def test_wrong_patch_is_not_reported_as_success_in_a_real_container(config, tmp_path, wrong_diff, expected_diff):
    orch, managers = orchestrator(config, [wrong_diff, expected_diff], tmp_path)
    sid = orch.create_session(FIXTURE)
    snap = orch.approve(sid, orch.start(sid).pending_patch.sha256)
    assert snap.state is State.WAITING_APPROVAL and snap.patch_attempts == 2  # retried, not "fixed"
    final = orch.approve(sid, snap.pending_patch.sha256)
    assert final.state is State.SUCCESS and len(managers.created) == 1
    assert not managers.created[0].container_present()
