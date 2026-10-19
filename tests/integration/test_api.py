"""The HTTP API end to end: real FastAPI app, real background execution, real git and pytest.

No Docker and no AWS: the sandbox is the LocalSandbox double and the engine is a FakeModel.
"""
from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import BUG_LINE, FIXTURE, SandboxFactory  # noqa: E402
from repodoctor.agent import FakeModel, SessionStore  # noqa: E402
from repodoctor.api.app import create_app  # noqa: E402
from repodoctor.api.schemas import ComponentStatus  # noqa: E402
from repodoctor.api.settings import ApiSettings  # noqa: E402

TERMINAL = {"SUCCESS", "FAILED", "REJECTED", "CANCELLED", "NO_FAILURE"}
ORIGIN = "http://localhost:5173"


@pytest.fixture
def make_client(tmp_path, sandbox_factory, expected_diff):
    opened = []

    def make(inject="default", *, store=None, factory=None, lifespan=False, sandbox_check=None, **settings_kw):
        settings_kw.setdefault("data_dir", tmp_path / "data")
        settings = ApiSettings(allowed_hosts=("testserver",), cors_origins=(ORIGIN,),
                               janitor_interval_seconds=0.2, **settings_kw)
        app = create_app(settings, store=store, sandbox_factory=factory or sandbox_factory,
                         engine=FakeModel([expected_diff]) if inject == "default" else inject,
                         sandbox_check=sandbox_check)
        client = TestClient(app)
        if lifespan:
            client.__enter__()
            opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def wait_for(client, sid, states, timeout=90):
    deadline = time.time() + timeout
    view = {}
    while time.time() < deadline:
        view = client.get(f"/api/debug-sessions/{sid}").json()
        if view["state"] in states:
            return view
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {states}; last view: {view}")


def sample_session(client):
    repo = client.post("/api/repositories/sample").json()
    session = client.post("/api/debug-sessions", json={"repository_id": repo["id"]})
    assert session.status_code == 201, session.text
    return repo, session.json()


def run_to_waiting(client):
    repo, session = sample_session(client)
    sid = session["session_id"]
    assert client.post(f"/api/debug-sessions/{sid}/start").status_code == 202
    return sid, wait_for(client, sid, {"WAITING_APPROVAL"} | TERMINAL)


def zip_of(path: Path, wrapper: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(path.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and ".pytest_cache" not in f.parts:
                rel = f.relative_to(path).as_posix()
                z.write(f, f"{wrapper}/{rel}" if wrapper else rel)
    return buf.getvalue()


def payment_source(sandbox_factory, index=0):
    return (Path(sandbox_factory.created[index].workspace) / "app" / "payment.py").read_text()


# ------------------------------------------------------------------ health
def test_health_is_ok_with_a_working_sandbox_and_engine(make_client):
    body = make_client().get("/api/health").json()
    assert body["status"] == "ok" and body["sandbox"]["ok"] and body["ai"]["ok"]
    assert body["engine"] == {"kind": "custom", "model_id": None, "simulated": False}
    assert body["version"] and body["max_active_sessions"] == 2 and body["active_sessions"] == 0


def test_health_degrades_with_plain_messages_when_docker_is_down(make_client):
    down = lambda: ComponentStatus(ok=False, message="Sandbox unavailable. Docker must be running.")  # noqa: E731
    body = make_client(sandbox_check=down).get("/api/health").json()
    assert body["status"] == "degraded" and body["sandbox"]["message"] == "Sandbox unavailable. Docker must be running."


def test_health_reports_a_missing_model_configuration(make_client, monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    body = make_client(inject=None).get("/api/health").json()
    assert body["status"] == "degraded" and not body["ai"]["ok"]
    assert body["ai"]["message"].startswith("AI service unavailable. Check AWS configuration.")
    assert body["engine"]["kind"] == "bedrock" and not body["engine"]["simulated"]


# ------------------------------------------------------------------ the happy path
def test_full_flow_over_http(make_client, sandbox_factory):
    client = make_client()
    repo, session = sample_session(client)
    sid = session["session_id"]
    assert session["state"] == "INITIALIZE" and session["repository_id"] == repo["id"]

    started = client.post(f"/api/debug-sessions/{sid}/start")
    assert started.status_code == 202  # returns immediately; the work happens in the background

    waiting = wait_for(client, sid, {"WAITING_APPROVAL"})
    patch = waiting["pending_patch"]
    assert waiting["ui_state"] == "awaiting approval" and patch["files"] == ["app/payment.py"]
    assert "user_id" in payment_source(sandbox_factory)  # nothing applied before approval

    approved = client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": patch["sha256"]})
    assert approved.status_code == 202
    final = wait_for(client, sid, TERMINAL)
    assert final["state"] == "SUCCESS" and final["ui_state"] == "success"
    report = final["report"]
    assert report["headline"].startswith("DEBUGGING COMPLETE")
    assert (report["before"]["failed"], report["before"]["passed"]) == (1, 31)
    assert (report["after"]["failed"], report["after"]["passed"]) == (0, 32)
    assert report["verification"]["verified"] is True and report["files_changed"] == ["app/payment.py"]
    assert sandbox_factory.created[0].destroyed


def test_approving_the_instant_a_session_is_waiting_never_loses_the_approval(make_client, monkeypatch):
    """Regression: the worker that reached WAITING_APPROVAL may still be finishing when the approval arrives."""
    client = make_client()
    orchestrator = client.app.state.orchestrator
    real_start = orchestrator.start

    def slow_to_return(sid):
        snapshot = real_start(sid)  # state is WAITING_APPROVAL from here on...
        time.sleep(1.5)             # ...but this worker is still "in flight" for a while
        return snapshot

    monkeypatch.setattr(orchestrator, "start", slow_to_return)
    repo, session = sample_session(client)
    sid = session["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    waiting = wait_for(client, sid, {"WAITING_APPROVAL"})
    assert client.app.state.runner.is_running(sid)  # the race window is open
    approved = client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": waiting["pending_patch"]["sha256"]})
    assert approved.status_code == 202, approved.text
    assert wait_for(client, sid, TERMINAL)["state"] == "SUCCESS"  # not stuck in APPLY_PATCH


def test_rejecting_the_instant_a_session_is_waiting_works_too(make_client, monkeypatch, sandbox_factory):
    client = make_client()
    orchestrator = client.app.state.orchestrator
    real_start = orchestrator.start

    def slow_to_return(sid):
        snapshot = real_start(sid)
        time.sleep(1.5)
        return snapshot

    monkeypatch.setattr(orchestrator, "start", slow_to_return)
    _, session = sample_session(client)
    sid = session["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    waiting = wait_for(client, sid, {"WAITING_APPROVAL"})
    assert client.app.state.runner.is_running(sid)
    rejected = client.post(f"/api/debug-sessions/{sid}/reject", json={"patch_sha256": waiting["pending_patch"]["sha256"]})
    assert rejected.status_code == 200 and rejected.json()["state"] == "REJECTED"
    assert "user_id" in payment_source(sandbox_factory)


def test_repository_detail_lists_files(make_client):
    client = make_client()
    repo = client.post("/api/repositories/sample").json()
    detail = client.get(f"/api/repositories/{repo['id']}").json()
    assert repo["kind"] == "sample" and repo["files"] == [] and detail["file_count"] == 10
    assert "app/payment.py" in detail["files"] and "tests/test_payment.py" in detail["files"]


def test_uploaded_zip_repository_works_end_to_end(make_client):
    client = make_client()
    uploaded = client.post("/api/repositories", files={"file": ("my-repo.zip", zip_of(FIXTURE, wrapper="repo-main"))})
    assert uploaded.status_code == 201, uploaded.text
    repo = uploaded.json()
    assert repo["kind"] == "upload" and repo["name"] == "my-repo.zip" and repo["file_count"] == 10
    sid = client.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    patch = wait_for(client, sid, {"WAITING_APPROVAL"})["pending_patch"]
    client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": patch["sha256"]})
    assert wait_for(client, sid, TERMINAL)["state"] == "SUCCESS"


# ------------------------------------------------------------------ approval is enforced by the backend
def test_approving_the_wrong_hash_is_refused_and_applies_nothing(make_client, sandbox_factory):
    client = make_client()
    sid, waiting = run_to_waiting(client)
    refused = client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": "f" * 64})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "patch_mismatch"
    assert client.get(f"/api/debug-sessions/{sid}").json()["state"] == "WAITING_APPROVAL"
    assert "user_id" in payment_source(sandbox_factory)


def test_cannot_approve_before_the_patch_exists(make_client):
    client = make_client()
    _, session = sample_session(client)
    refused = client.post(f"/api/debug-sessions/{session['session_id']}/approve", json={"patch_sha256": "a" * 64})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "invalid_state"


def test_approval_cannot_be_replayed(make_client):
    client = make_client()
    sid, waiting = run_to_waiting(client)
    sha = waiting["pending_patch"]["sha256"]
    assert client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": sha}).status_code == 202
    wait_for(client, sid, TERMINAL)
    again = client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": sha})
    assert again.status_code == 409


def test_reject_never_applies_the_patch(make_client, sandbox_factory):
    client = make_client()
    sid, waiting = run_to_waiting(client)
    rejected = client.post(f"/api/debug-sessions/{sid}/reject", json={"patch_sha256": waiting["pending_patch"]["sha256"]})
    assert rejected.status_code == 200
    view = rejected.json()
    assert view["state"] == "REJECTED" and view["ui_state"] == "cancelled" and view["report"]["verification"] is None
    assert "user_id" in payment_source(sandbox_factory) and sandbox_factory.created[0].destroyed


def test_cancel_a_waiting_session(make_client, sandbox_factory):
    client = make_client()
    sid, _ = run_to_waiting(client)
    assert client.post(f"/api/debug-sessions/{sid}/cancel").json()["state"] == "CANCELLED"
    assert sandbox_factory.created[0].destroyed
    assert client.post(f"/api/debug-sessions/{sid}/cancel").status_code == 409


def test_start_twice_is_refused(make_client):
    client = make_client()
    sid, _ = run_to_waiting(client)
    again = client.post(f"/api/debug-sessions/{sid}/start")
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_state"


# ------------------------------------------------------------------ validation and errors
@pytest.mark.parametrize("path", ["/api/debug-sessions/{sid}", "/api/repositories/{sid}"])
def test_unknown_ids_are_404_and_malformed_ids_are_422(make_client, path):
    client = make_client()
    assert client.get(path.format(sid="0" * 12)).status_code == 404
    bad = client.get(path.format(sid="not-an-id"))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("body", [{}, {"repository_id": "short"}, {"repository_id": "0" * 12, "extra": 1},
                                  {"repository_id": "ZZZZZZZZZZZZ"}])
def test_session_creation_validates_input(make_client, body):
    assert make_client().post("/api/debug-sessions", json=body).status_code == 422


def test_creating_a_session_for_an_unknown_repository_is_404(make_client):
    refused = make_client().post("/api/debug-sessions", json={"repository_id": "0" * 12})
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "repository_not_found"


@pytest.mark.parametrize("body", [{}, {"patch_sha256": "abc"}, {"patch_sha256": "G" * 64}, {"patch_sha256": "a" * 64, "x": 1}])
def test_approve_and_reject_validate_the_hash_format(make_client, body):
    client = make_client()
    _, session = sample_session(client)
    for action in ("approve", "reject"):
        assert client.post(f"/api/debug-sessions/{session['session_id']}/{action}", json=body).status_code == 422


def test_active_session_limit(make_client):
    client = make_client(max_active_sessions=1)
    repo = client.post("/api/repositories/sample").json()
    assert client.post("/api/debug-sessions", json={"repository_id": repo["id"]}).status_code == 201
    refused = client.post("/api/debug-sessions", json={"repository_id": repo["id"]})
    assert refused.status_code == 429 and refused.json()["error"]["code"] == "too_many_sessions"


# ------------------------------------------------------------------ uploads
def test_upload_errors_are_clear(make_client):
    client = make_client(max_upload_bytes=2000)
    not_zip = client.post("/api/repositories", files={"file": ("x.zip", b"hello")})
    assert not_zip.status_code == 400 and not_zip.json()["error"]["code"] == "invalid_zip"
    too_big = client.post("/api/repositories", files={"file": ("x.zip", b"\0" * 5000)})
    assert too_big.status_code == 413 and too_big.json()["error"]["code"] == "upload_too_large"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.py", "x")
    evil = client.post("/api/repositories", files={"file": ("evil.zip", buf.getvalue())})
    assert evil.status_code == 400 and evil.json()["error"]["code"] == "unsafe_archive"
    empty = io.BytesIO()
    zipfile.ZipFile(empty, "w").close()
    assert client.post("/api/repositories", files={"file": ("e.zip", empty.getvalue())}).json()["error"]["code"] == "empty_repository"


def test_failed_uploads_leave_nothing_behind(make_client, tmp_path):
    client = make_client()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("ok.py", "1")
        z.writestr("../evil.py", "x")
    client.post("/api/repositories", files={"file": ("evil.zip", buf.getvalue())})
    assert list((tmp_path / "data" / "repos").iterdir()) == []


def test_upload_without_a_file_is_a_clean_422(make_client):
    assert make_client().post("/api/repositories").status_code == 422


# ------------------------------------------------------------------ non-debuggable cases surface as clean states
def test_a_repository_with_no_python_files_fails_with_a_readable_message(make_client):
    client = make_client()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("README.md", "# docs only\n")
    repo = client.post("/api/repositories", files={"file": ("docs.zip", buf.getvalue())}).json()
    sid = client.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    final = wait_for(client, sid, TERMINAL)
    assert final["state"] == "FAILED" and final["error_code"] == "invalid_project" and final["ui_state"] == "failed"


def test_docker_down_surfaces_the_standard_message(make_client):
    from repodoctor.sandbox.errors import DockerUnavailableError

    def broken():
        raise DockerUnavailableError("Sandbox unavailable. Docker must be running.")

    client = make_client(factory=broken)
    _, session = sample_session(client)
    client.post(f"/api/debug-sessions/{session['session_id']}/start")
    final = wait_for(client, session["session_id"], TERMINAL)
    assert final["state"] == "FAILED" and "Docker must be running" in final["message"]


def test_unconfigured_model_is_refused_before_any_work_starts(make_client, monkeypatch, sandbox_factory):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    client = make_client(inject=None)
    _, session = sample_session(client)
    refused = client.post(f"/api/debug-sessions/{session['session_id']}/start")
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "ai_unavailable"
    assert refused.json()["error"]["message"].startswith("AI service unavailable. Check AWS configuration.")
    assert sandbox_factory.created == []  # no container was created for a session that could never succeed


# ------------------------------------------------------------------ demo mode is labelled
def test_demo_engine_is_labelled_simulated_and_limited_to_the_sample(make_client):
    client = make_client(inject=None, engine="demo")  # engine="demo" is the ApiSettings switch
    health = client.get("/api/health").json()
    assert health["engine"] == {"kind": "demo", "model_id": None, "simulated": True}
    assert "simulated" in health["ai"]["message"].lower()

    upload = client.post("/api/repositories", files={"file": ("r.zip", zip_of(FIXTURE))}).json()
    refused = client.post("/api/debug-sessions", json={"repository_id": upload["id"]})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "demo_mode"

    repo = client.post("/api/repositories/sample").json()
    session = client.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()
    assert session["engine"]["simulated"] is True
    sid = session["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    waiting = wait_for(client, sid, {"WAITING_APPROVAL"})
    assert "Scripted demo patch (simulated)" in waiting["pending_patch"]["summary"]
    client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": waiting["pending_patch"]["sha256"]})
    assert wait_for(client, sid, TERMINAL)["state"] == "SUCCESS"


def test_the_demo_diff_has_correct_line_numbers_even_if_the_sample_was_reformatted(make_client, monkeypatch, tmp_path):
    import shutil
    import repodoctor.api.app as app_module
    sample = tmp_path / "reformatted_sample"
    shutil.copytree(FIXTURE, sample, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    source = sample / "app" / "payment.py"
    source.write_text(source.read_text().replace('gateway."""\nfrom __future__', 'gateway."""\n\nfrom __future__', 1))  # what Black does
    bug_line = next(n for n, line in enumerate(source.read_text().splitlines(), 1) if "charge(user_id" in line)
    monkeypatch.setattr(app_module, "SAMPLE_REPO", sample)

    client = make_client(inject=None, engine="demo")
    repo = client.post("/api/repositories/sample").json()
    sid = client.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    waiting = wait_for(client, sid, {"WAITING_APPROVAL"})
    diff = waiting["pending_patch"]["diff"]
    assert f"@@ -{bug_line - 3},7 +{bug_line - 3},7 @@" in diff  # three lines of context before the bug, wherever it is
    client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": waiting["pending_patch"]["sha256"]})
    assert wait_for(client, sid, TERMINAL)["state"] == "SUCCESS"


def test_the_demo_engine_can_serve_several_sessions(make_client):
    client = make_client(inject=None, engine="demo", max_active_sessions=3)
    for _ in range(2):
        repo = client.post("/api/repositories/sample").json()
        sid = client.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()["session_id"]
        client.post(f"/api/debug-sessions/{sid}/start")
        assert wait_for(client, sid, {"WAITING_APPROVAL"})["pending_patch"]["files"] == ["app/payment.py"]


def test_demo_with_a_wrong_scripted_patch_ends_in_an_honest_failure(make_client, monkeypatch):
    wrong = Path(__file__).resolve().parents[2] / "fixtures" / "expected_patches" / "wrong_patch.diff"
    monkeypatch.setenv("REPODOCTOR_DEMO_PATCH", str(wrong))
    client = make_client(inject=None, engine="demo")
    repo = client.post("/api/repositories/sample").json()
    sid = client.post("/api/debug-sessions", json={"repository_id": repo["id"]}).json()["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    waiting = wait_for(client, sid, {"WAITING_APPROVAL"})
    client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": waiting["pending_patch"]["sha256"]})
    final = wait_for(client, sid, TERMINAL)
    assert final["state"] == "FAILED" and final["message"] == "I could not safely verify a fix."
    report = final["report"]
    assert report["verification"]["verified"] is False and report["verification"]["reason_code"] == "tests_still_failing"
    assert "DEBUGGING COMPLETE" not in report["headline"]
    assert [f["reason_code"] for f in report["attempt_feedback"]][0] == "tests_still_failing"


# ------------------------------------------------------------------ browser/network hardening
def test_cross_site_writes_are_rejected_but_same_origin_and_scripts_are_fine(make_client):
    client = make_client()
    hostile = client.post("/api/repositories/sample", headers={"Origin": "https://evil.example"})
    assert hostile.status_code == 403 and hostile.json()["error"]["code"] == "forbidden_origin"
    assert client.post("/api/repositories/sample", headers={"Origin": ORIGIN}).status_code == 201
    assert client.post("/api/repositories/sample").status_code == 201  # curl/scripts send no Origin
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 200


@pytest.mark.parametrize("origin", ["http://testserver", "https://testserver", "http://TESTSERVER"])
def test_same_origin_posts_from_the_served_ui_are_accepted(make_client, origin):
    """Regression: browsers send Origin on every POST, even same-origin. The UI served by this server must work."""
    client = make_client()
    assert client.post("/api/repositories/sample", headers={"Origin": origin}).status_code == 201


@pytest.mark.parametrize("origin", ["http://testserver:9999", "http://evil.testserver", "http://testserver.evil.example",
                                    "null", "file://", "http://", "https://evil.example"])
def test_look_alike_and_opaque_origins_are_still_rejected(make_client, origin):
    response = make_client().post("/api/repositories/sample", headers={"Origin": origin})
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden_origin"


def test_a_hostile_page_cannot_ride_on_a_matching_host_header_alone(make_client):
    # the Host header must itself be one we serve; a rebinding page that names its own host never gets this far
    client = make_client()
    response = client.post("/api/repositories/sample", headers={"Origin": "http://evil.example", "Host": "evil.example"})
    assert response.status_code == 400  # rejected by the host check before the origin check


def test_unknown_host_headers_are_rejected(make_client):
    client = make_client()
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400


def test_cors_allows_only_the_configured_origin(make_client):
    client = make_client()
    ok = client.get("/api/health", headers={"Origin": ORIGIN})
    assert ok.headers.get("access-control-allow-origin") == ORIGIN
    other = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in other.headers


# ------------------------------------------------------------------ server-sent events
def parse_sse(text: str):
    events = []
    for block in text.strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            fields[key] = value
        if fields:
            events.append(fields)
    return events


def test_event_stream_replays_the_timeline_and_ends_with_a_snapshot(make_client):
    client = make_client()
    sid, waiting = run_to_waiting(client)
    client.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": waiting["pending_patch"]["sha256"]})
    wait_for(client, sid, TERMINAL)

    response = client.get(f"/api/debug-sessions/{sid}/events")
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    steps = [e for e in events if e["event"] == "step"]
    ids = [int(e["id"]) for e in steps]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    messages = [json.loads(e["data"])["message"] for e in steps]
    assert any("Reproduced failure: 1 failed, 31 passed" in m for m in messages)
    assert any(f"Identified root cause in app/payment.py:{BUG_LINE}" in m for m in messages)
    assert any("Verification passed" in m for m in messages)
    final = events[-1]
    assert final["event"] == "snapshot" and json.loads(final["data"])["state"] == "SUCCESS"


def test_event_stream_resumes_after_a_given_event(make_client):
    client = make_client()
    sid, _ = run_to_waiting(client)
    all_steps = [e for e in parse_sse(client.get(f"/api/debug-sessions/{sid}/events?follow=false").text) if e["event"] == "step"]
    cut = int(all_steps[2]["id"])
    for request in (client.get(f"/api/debug-sessions/{sid}/events?follow=false&after={cut}"),
                    client.get(f"/api/debug-sessions/{sid}/events?follow=false", headers={"Last-Event-ID": str(cut)})):
        later = [e for e in parse_sse(request.text) if e["event"] == "step"]
        assert [int(e["id"]) for e in later] == [int(e["id"]) for e in all_steps if int(e["id"]) > cut]


def test_following_a_live_session_ends_when_it_finishes(make_client):
    client = make_client()
    _, session = sample_session(client)
    sid = session["session_id"]
    client.post(f"/api/debug-sessions/{sid}/start")
    wait_for(client, sid, {"WAITING_APPROVAL"})
    client.post(f"/api/debug-sessions/{sid}/cancel")
    text = client.get(f"/api/debug-sessions/{sid}/events").text  # follow=true: finite because the session is over
    assert parse_sse(text)[-1]["event"] == "snapshot" and json.loads(parse_sse(text)[-1]["data"])["state"] == "CANCELLED"


def test_events_for_unknown_sessions_are_404(make_client):
    assert make_client().get(f"/api/debug-sessions/{'0' * 12}/events").status_code == 404


# ------------------------------------------------------------------ restart recovery and cleanup
def test_waiting_session_survives_a_server_restart(tmp_path, make_client, expected_diff):
    db = tmp_path / "shared.sqlite3"
    first = make_client(store=SessionStore(db))
    sid, waiting = run_to_waiting(first)
    sha = waiting["pending_patch"]["sha256"]
    first.app.state.store.close()  # --- the server stops ---

    factory_b = SandboxFactory(tmp_path / "after_restart")
    second = make_client(store=SessionStore(db), factory=factory_b, lifespan=True)
    assert second.get(f"/api/debug-sessions/{sid}").json()["state"] == "WAITING_APPROVAL"
    assert second.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": "0" * 64}).status_code == 409
    assert second.post(f"/api/debug-sessions/{sid}/approve", json={"patch_sha256": sha}).status_code == 202
    final = wait_for(second, sid, TERMINAL)
    assert final["state"] == "SUCCESS" and len(factory_b.created) == 1


def test_sessions_that_were_mid_run_are_marked_failed_after_a_restart(make_client, tmp_path):
    store = SessionStore(tmp_path / "s.sqlite3")
    sid = store.create_session(str(FIXTURE))
    from repodoctor.agent.states import State
    store.set_state(sid, expected=State.INITIALIZE, new=State.RUN_TESTS)
    client = make_client(store=store, lifespan=True)
    view = client.get(f"/api/debug-sessions/{sid}").json()
    assert view["state"] == "FAILED" and view["error_code"] == "server_restarted"
    assert "restarted" in view["message"]


def test_abandoned_approvals_expire_and_release_their_container(make_client, sandbox_factory):
    client = make_client(waiting_ttl_seconds=1, lifespan=True)
    sid, _ = run_to_waiting(client)
    final = wait_for(client, sid, {"CANCELLED"}, timeout=20)
    assert "expired" in final["message"].lower()
    assert final["report"] is not None  # visible state never lags its report
    deadline = time.time() + 10  # the container is released just after the state becomes visible
    while not sandbox_factory.created[0].destroyed and time.time() < deadline:
        time.sleep(0.05)
    assert sandbox_factory.created[0].destroyed
