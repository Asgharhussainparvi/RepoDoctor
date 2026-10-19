"""The API also serves the built web UI (frontend/dist) when it exists."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from repodoctor.agent import FakeModel  # noqa: E402
from repodoctor.api.app import create_app  # noqa: E402
from repodoctor.api.settings import ApiSettings  # noqa: E402


def client_for(tmp_path, sandbox_factory, dist=None):
    settings = ApiSettings(data_dir=tmp_path / "data", allowed_hosts=("testserver",))
    app = create_app(
        settings,
        engine=FakeModel([]),
        sandbox_factory=sandbox_factory,
        frontend_dir=dist,
    )
    return TestClient(app)


@pytest.fixture
def dist(tmp_path):
    folder = tmp_path / "dist"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text(
        "<!doctype html><title>RepoDoctor</title><div id=root></div>"
    )
    (folder / "assets" / "app.js").write_text("console.log('ui')")
    return folder


def test_the_built_ui_is_served_at_the_root(tmp_path, sandbox_factory, dist):
    client = client_for(tmp_path, sandbox_factory, dist)
    root = client.get("/")
    assert (
        root.status_code == 200
        and "<div id=root>" in root.text
        and root.headers["content-type"].startswith("text/html")
    )
    assert client.get("/assets/app.js").text == "console.log('ui')"
    assert client.get("/assets/missing.js").status_code == 404


def test_api_routes_still_win_over_the_ui(tmp_path, sandbox_factory, dist):
    client = client_for(tmp_path, sandbox_factory, dist)
    assert client.get("/api/health").json()["version"]
    assert client.post("/api/repositories/sample").status_code == 201


def test_unknown_api_paths_return_the_json_envelope_not_the_ui(
    tmp_path, sandbox_factory, dist
):
    client = client_for(tmp_path, sandbox_factory, dist)
    for path in ("/api/nope", "/api/debug-sessions/x/y/z"):
        response = client.get(path)
        assert (
            response.status_code == 404
            and response.json()["error"]["code"] == "not_found"
        )


def test_without_a_build_the_root_explains_how_to_build_it(tmp_path, sandbox_factory):
    client = client_for(tmp_path, sandbox_factory, tmp_path / "does-not-exist")
    body = client.get("/").json()
    assert "npm run build" in body["build"] and body["api_docs"] == "/docs"
    assert client.get("/api/health").status_code == 200


def test_the_ui_is_covered_by_the_host_check(tmp_path, sandbox_factory, dist):
    assert (
        client_for(tmp_path, sandbox_factory, dist)
        .get("/", headers={"host": "evil.example"})
        .status_code
        == 400
    )
