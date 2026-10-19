"""A REAL uvicorn server on a real socket, driven over HTTP, including live SSE streaming.

(No Docker, no AWS: LocalSandbox + FakeModel.) TestClient cannot prove live streaming; this can.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")
import uvicorn  # noqa: E402

from repodoctor.agent import FakeModel  # noqa: E402
from repodoctor.api.app import create_app  # noqa: E402
from repodoctor.api.settings import ApiSettings  # noqa: E402


@pytest.fixture
def live_server(tmp_path, sandbox_factory, expected_diff):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(ApiSettings(data_dir=tmp_path / "data"), engine=FakeModel([expected_diff]),
                     sandbox_factory=sandbox_factory)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def sse_events(lines):
    event = {}
    for line in lines:
        if line == "":
            if event:
                yield event
            event = {}
        elif not line.startswith(":"):
            key, _, value = line.partition(": ")
            event[key] = value


def test_live_stream_follows_a_session_from_start_to_verified_success(live_server):
    with httpx.Client(base_url=live_server, timeout=60) as api, httpx.Client(base_url=live_server, timeout=60) as stream_client:
        repo = api.post("/api/repositories/sample").json()
        sid = api.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()["session_id"]

        seen, approved, final = [], False, None
        with stream_client.stream("GET", f"/api/debug-sessions/{sid}/events") as stream:
            assert stream.headers["content-type"].startswith("text/event-stream")
            api.post(f"/api/debug-sessions/{sid}/start")  # the stream is already open: events arrive live
            for event in sse_events(stream.iter_lines()):
                if event["event"] == "step":
                    data = json.loads(event["data"])
                    seen.append(data["state"])
                    if data["state"] == "WAITING_APPROVAL" and not approved:
                        approved = True
                        patch = api.get(f"/api/debug-sessions/{sid}").json()["pending_patch"]
                        assert api.post(f"/api/debug-sessions/{sid}/approve",
                                        json={"patch_sha256": patch["sha256"]}).status_code == 202
                elif event["event"] == "snapshot":
                    final = json.loads(event["data"])
                    break
        assert final["state"] == "SUCCESS" and final["report"]["verification"]["verified"] is True
        assert seen.index("RUN_TESTS") < seen.index("WAITING_APPROVAL") < seen.index("VERIFY") < seen.index("SUCCESS")


def test_real_server_rejects_bad_hosts_and_serves_docs(live_server):
    port = live_server.rsplit(":", 1)[1]
    assert httpx.get(live_server + "/api/health").status_code == 200
    assert httpx.get(live_server + "/api/health", headers={"host": "evil.example"}).status_code == 400
    assert httpx.get(live_server + "/openapi.json").json()["info"]["title"] == "RepoDoctor"


def test_the_origin_header_a_real_browser_sends_is_accepted_for_the_page_this_server_serves(live_server):
    """Regression: the built UI at http://127.0.0.1:PORT posts with Origin == that same address."""
    same = {"Origin": live_server}
    assert httpx.post(live_server + "/api/repositories/sample", headers=same).status_code == 201
    other_port = live_server.rsplit(":", 1)[0] + ":1"
    assert httpx.post(live_server + "/api/repositories/sample", headers={"Origin": other_port}).status_code == 403
    assert httpx.post(live_server + "/api/repositories/sample", headers={"Origin": "https://evil.example"}).status_code == 403
