"""The HTTP API against the REAL Docker sandbox (FakeModel, no AWS).

Run: python -m pytest -m docker tests/e2e/test_api_docker.py -v
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from repodoctor.agent import FakeModel  # noqa: E402
from repodoctor.api.app import create_app  # noqa: E402
from repodoctor.api.settings import ApiSettings  # noqa: E402
from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager  # noqa: E402

pytestmark = pytest.mark.docker
TERMINAL = {"SUCCESS", "FAILED", "REJECTED", "CANCELLED", "NO_FAILURE"}


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
        pytest.fail(
            f"Sandbox image {config.image!r} is missing. Run: python -m repodoctor.sandbox build"
        )


@pytest.fixture
def api(config, tmp_path, expected_diff):
    managers = []

    def factory():
        manager = SandboxManager(config)
        managers.append(manager)
        return manager

    settings = ApiSettings(data_dir=tmp_path / "data", allowed_hosts=("testserver",))
    with TestClient(
        create_app(settings, engine=FakeModel([expected_diff]), sandbox_factory=factory)
    ) as client:
        client.managers = managers
        yield client


def wait_for(client, sid, states, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/api/debug-sessions/{sid}").json()
        if view["state"] in states:
            return view
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {states}")


def test_health_sees_real_docker_and_the_image(api):
    body = api.get("/api/health").json()
    assert body["sandbox"]["ok"], body["sandbox"]["message"]


def test_full_flow_in_a_real_container(api):
    repo = api.post("/api/repositories/sample").json()
    sid = api.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()[
        "session_id"
    ]
    api.post(f"/api/debug-sessions/{sid}/start")
    waiting = wait_for(api, sid, {"WAITING_APPROVAL"} | TERMINAL)
    assert waiting["state"] == "WAITING_APPROVAL", waiting["message"]
    assert api.managers[0].container_present()
    api.post(
        f"/api/debug-sessions/{sid}/approve",
        json={"patch_sha256": waiting["pending_patch"]["sha256"]},
    )
    final = wait_for(api, sid, TERMINAL)
    assert final["state"] == "SUCCESS" and final["report"]["verification"]["verified"]
    assert (final["report"]["after"]["failed"], final["report"]["after"]["passed"]) == (
        0,
        32,
    )
    assert not api.managers[0].container_present()


def test_cancel_and_reject_release_the_container(api):
    repo = api.post("/api/repositories/sample").json()
    sid = api.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()[
        "session_id"
    ]
    api.post(f"/api/debug-sessions/{sid}/start")
    waiting = wait_for(api, sid, {"WAITING_APPROVAL"} | TERMINAL)
    assert (
        api.post(
            f"/api/debug-sessions/{sid}/reject",
            json={"patch_sha256": waiting["pending_patch"]["sha256"]},
        ).json()["state"]
        == "REJECTED"
    )
    assert not api.managers[0].container_present()
