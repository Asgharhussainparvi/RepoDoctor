"""The FastAPI application. Thin: every decision lives in the Orchestrator.

Security posture (local tool, no accounts): binds to 127.0.0.1, rejects unknown Host headers
(DNS rebinding) and cross-site POSTs (Origin check), never accepts a command or a host path from
a client, and extracts uploaded archives defensively. Do NOT expose it to a network.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import FastAPI, File, Header, Path as PathParam, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from repodoctor import __version__
from repodoctor.agent import (
    AgentConfig, BedrockEngine, BedrockSettings, Orchestrator, OrchestratorError, SessionStore, TERMINAL_STATES, State,
)
from repodoctor.agent.schemas import SessionSnapshot
from repodoctor.sandbox import SandboxConfig, SandboxError, SandboxManager
from repodoctor.sandbox.manager import EXCLUDED_NAMES

from .demo import ROOT, SAMPLE_REPO, DemoEngine, UnconfiguredEngine
from .repos import RepoError, RepoLimits, extract_zip, list_repo_files
from .runner import BackgroundRunner
from .schemas import (
    ID_PATTERN, ComponentStatus, CreateSessionRequest, EngineInfo, HealthView, PatchDecisionRequest,
    RepositoryView, SessionView,
)
from .settings import ApiSettings

logger = logging.getLogger("repodoctor.api")

AI_UNAVAILABLE = "AI service unavailable. Check AWS configuration."
_HTTP_FOR_CODE = {"session_not_found": 404, "invalid_state": 409, "patch_mismatch": 409, "session_busy": 409}


class ApiError(RepoError):
    """Any API-level refusal (shares the envelope with RepoError)."""


def _envelope(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def _default_ai_check(settings_error: str | None, model_id: str | None) -> ComponentStatus:
    """Configuration check only. It never calls Bedrock, so it never costs money."""
    if settings_error:
        return ComponentStatus(ok=False, message=f"{AI_UNAVAILABLE} ({settings_error})")
    if importlib.util.find_spec("strands") is None:
        return ComponentStatus(ok=False, message=f"{AI_UNAVAILABLE} (Strands is not installed: pip install -r requirements-aws.txt)")
    has_credentials = bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or os.environ.get("AWS_ACCESS_KEY_ID")
                           or os.environ.get("AWS_PROFILE") or (Path.home() / ".aws" / "credentials").exists())
    if not has_credentials:
        return ComponentStatus(ok=False, message=f"{AI_UNAVAILABLE} (no AWS credentials or Bedrock API key found)")
    return ComponentStatus(ok=True, message=f"Configured for {model_id}. Access is verified on the first real run.")


def create_app(
    settings: ApiSettings | None = None, *, store: SessionStore | None = None, engine=None,
    sandbox_factory: Callable[[], object] | None = None, sandbox_check: Callable[[], ComponentStatus] | None = None,
    frontend_dir: Path | None = None,
) -> FastAPI:
    settings = settings or ApiSettings.from_env()
    data_dir = Path(settings.data_dir).resolve()
    repos_dir = data_dir / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)
    store = store or SessionStore(data_dir / "repodoctor.sqlite3")

    # ---- engine selection ----
    settings_error: str | None = None
    if engine is not None:
        info, engine_ok = EngineInfo(kind="custom", simulated=False), True
    elif settings.engine == "demo":
        engine, info, engine_ok = DemoEngine(os.environ.get("REPODOCTOR_DEMO_PATCH")), EngineInfo(kind="demo", simulated=True), True
    else:
        try:
            bedrock = BedrockSettings.from_env()
            engine, info, engine_ok = BedrockEngine(bedrock), EngineInfo(kind="bedrock", model_id=bedrock.model_id), True
        except ValueError as exc:
            settings_error = str(exc).split(".")[0]
            engine, info, engine_ok = UnconfiguredEngine(), EngineInfo(kind="bedrock"), False

    if sandbox_factory is None:
        sandbox_config = SandboxConfig.from_env()
        sandbox_factory = lambda: SandboxManager(sandbox_config)  # noqa: E731

        def sandbox_check() -> ComponentStatus:
            try:
                SandboxManager.check_docker(sandbox_config)
                if not SandboxManager.image_exists(sandbox_config):
                    return ComponentStatus(ok=False, message="Sandbox image missing. Run: python -m repodoctor.sandbox build")
                return ComponentStatus(ok=True, message="Docker is running and the sandbox image is present.")
            except SandboxError as exc:
                return ComponentStatus(ok=False, message=str(exc))
    elif sandbox_check is None:
        sandbox_check = lambda: ComponentStatus(ok=True, message="Custom sandbox.")  # noqa: E731

    orchestrator = Orchestrator(store, engine, sandbox_factory, config=AgentConfig())

    def on_background_error(sid: str, exc: BaseException) -> None:
        if not isinstance(exc, OrchestratorError):  # precondition races are not session failures
            orchestrator.fail_session(sid, "internal_error", "Unexpected internal error. The session was stopped.")

    runner = BackgroundRunner(settings.max_active_sessions, on_background_error)
    limits = RepoLimits(settings.max_repo_files, settings.max_unpacked_bytes)
    health_cache: dict = {"at": 0.0, "value": None}
    health_lock = threading.Lock()

    # ---- helpers ----
    def repo_view(row, with_files: bool = False) -> RepositoryView:
        files = list_repo_files(Path(row.path)) if with_files else []
        return RepositoryView(id=row.id, name=row.name, kind=row.kind, file_count=row.file_count,
                              total_bytes=row.total_bytes, created_at=row.created_at, files=files)

    def session_view(sid: str) -> SessionView:
        snap = orchestrator.get(sid)
        row = store.get_session(sid)
        return SessionView(**{k: getattr(snap, k) for k in SessionSnapshot.model_fields},
                           repository_id=row.repository_id, engine=info)

    def require_repository(rid: str):
        try:
            return store.get_repository(rid)
        except KeyError as exc:
            raise ApiError("repository_not_found", f"No repository {rid!r}.", 404) from exc

    async def read_limited(file: UploadFile, limit: int) -> bytes:
        chunks, size = [], 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ApiError("upload_too_large", f"The upload is larger than {limit} bytes.", 413)
            chunks.append(chunk)
        return b"".join(chunks)

    # ---- lifecycle ----
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recovered = orchestrator.recover_interrupted()
        if recovered:
            logger.warning("recovered_interrupted_sessions ids=%s", recovered)

        async def janitor() -> None:
            while True:
                await asyncio.sleep(settings.janitor_interval_seconds)
                try:
                    expired = await asyncio.to_thread(orchestrator.expire_stale_waiting, settings.waiting_ttl_seconds)
                    if expired:
                        logger.info("expired_waiting_sessions ids=%s", expired)
                except Exception:  # noqa: BLE001
                    logger.exception("janitor_error")

        task = asyncio.create_task(janitor())
        try:
            yield
        finally:
            task.cancel()
            runner.shutdown()
            orchestrator.shutdown()

    app = FastAPI(title="RepoDoctor", version=__version__, lifespan=lifespan)
    app.state.orchestrator, app.state.store, app.state.runner = orchestrator, store, runner

    # ---- middleware ----
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "Last-Event-ID"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    def origin_allowed(origin: str, host: str) -> bool:
        """Same-origin (the page was served by this very server) or an explicitly configured origin.

        Browsers send an Origin header on every POST, including same-origin ones, so the built UI served from
        http://127.0.0.1:8000 sends exactly that. A hostile page sends ITS OWN origin, which can never equal our Host;
        a DNS-rebinding page is stopped earlier by the Host check.
        """
        if origin in settings.cors_origins:
            return True
        return bool(host) and urlparse(origin).netloc.lower() == host.lower()

    @app.middleware("http")
    async def reject_cross_site_writes(request: Request, call_next):
        origin = request.headers.get("origin")
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and origin
                and not origin_allowed(origin, request.headers.get("host", ""))):
            return JSONResponse(_envelope("forbidden_origin", "Requests from this origin are not allowed."), 403)
        return await call_next(request)

    # ---- errors: one envelope everywhere ----
    @app.exception_handler(RepoError)
    async def _repo_error(request: Request, exc: RepoError):
        return JSONResponse(_envelope(exc.code, str(exc)), exc.status)

    @app.exception_handler(OrchestratorError)
    async def _orchestrator_error(request: Request, exc: OrchestratorError):
        return JSONResponse(_envelope(exc.code, str(exc)), _HTTP_FOR_CODE.get(exc.code, 400))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors())
        return JSONResponse(_envelope("invalid_request", detail or "Invalid request."), 422)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.exception("unhandled_error path=%s", request.url.path)
        return JSONResponse(_envelope("internal_error", "Unexpected internal error."), 500)

    # ================================================================ routes
    @app.get("/api/health", response_model=HealthView)
    def health() -> HealthView:
        with health_lock:
            if health_cache["value"] is None or time.monotonic() - health_cache["at"] > 5:
                health_cache.update(at=time.monotonic(), value=sandbox_check())
            sandbox = health_cache["value"]
        if info.kind == "demo":
            ai = ComponentStatus(ok=True, message="Demo mode: the patch is scripted (simulated), not written by a model.")
        elif info.kind == "custom":
            ai = ComponentStatus(ok=True, message="Custom engine.")
        else:
            ai = _default_ai_check(settings_error, info.model_id)
        return HealthView(status="ok" if sandbox.ok and ai.ok else "degraded", version=__version__, sandbox=sandbox,
                          ai=ai, engine=info, active_sessions=store.count_active(),
                          max_active_sessions=settings.max_active_sessions)

    @app.post("/api/repositories", response_model=RepositoryView, status_code=201)
    async def upload_repository(file: UploadFile = File(...)) -> RepositoryView:
        data = await read_limited(file, settings.max_upload_bytes)
        rid = uuid.uuid4().hex[:12]
        dest = repos_dir / rid
        try:
            stats = await asyncio.to_thread(extract_zip, io.BytesIO(data), dest, limits)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        name = (Path((file.filename or "upload.zip").replace("\\", "/")).name or "upload.zip")[:100]
        return repo_view(store.create_repository(rid=rid, name=name, kind="upload", path=str(dest),
                                                 file_count=stats.file_count, total_bytes=stats.total_bytes))

    @app.post("/api/repositories/sample", response_model=RepositoryView, status_code=201)
    def register_sample() -> RepositoryView:
        rid = uuid.uuid4().hex[:12]
        dest = repos_dir / rid
        shutil.copytree(SAMPLE_REPO, dest, ignore=shutil.ignore_patterns(*EXCLUDED_NAMES))
        files = [p for p in dest.rglob("*") if p.is_file()]
        return repo_view(store.create_repository(rid=rid, name="buggy_payment_app (sample)", kind="sample",
                                                 path=str(dest), file_count=len(files),
                                                 total_bytes=sum(p.stat().st_size for p in files)))

    @app.get("/api/repositories/{rid}", response_model=RepositoryView)
    def get_repository(rid: str = PathParam(pattern=ID_PATTERN)) -> RepositoryView:
        return repo_view(require_repository(rid), with_files=True)

    @app.post("/api/debug-sessions", response_model=SessionView, status_code=201)
    def create_session(body: CreateSessionRequest) -> SessionView:
        repo = require_repository(body.repository_id)
        if info.kind == "demo" and repo.kind != "sample":
            raise ApiError("demo_mode", "Demo mode only works on the sample repository.", 400)
        if store.count_active() >= settings.max_active_sessions:
            raise ApiError("too_many_sessions",
                           "Too many active debugging sessions. Finish or cancel one first.", 429)
        sid = orchestrator.create_session(repo.path)
        store.set_repository(sid, repo.id)
        return session_view(sid)

    @app.get("/api/debug-sessions/{sid}", response_model=SessionView)
    def get_session(sid: str = PathParam(pattern=ID_PATTERN)) -> SessionView:
        return session_view(sid)

    @app.post("/api/debug-sessions/{sid}/start", response_model=SessionView, status_code=202)
    def start_session(sid: str = PathParam(pattern=ID_PATTERN)) -> SessionView:
        snap = orchestrator.get(sid)
        if snap.state is not State.INITIALIZE:
            raise OrchestratorError("invalid_state", f"Session is {snap.state.value}; it was already started.")
        if not engine_ok:
            raise ApiError("ai_unavailable", f"{AI_UNAVAILABLE} ({settings_error})", 503)
        try:
            runner.submit(sid, orchestrator.start, sid)
        except RuntimeError as exc:
            raise OrchestratorError("session_busy", "This session is already being processed.") from exc
        return session_view(sid)

    @app.post("/api/debug-sessions/{sid}/approve", response_model=SessionView, status_code=202)
    def approve_patch(body: PatchDecisionRequest, sid: str = PathParam(pattern=ID_PATTERN)) -> SessionView:
        orchestrator.record_approval(sid, body.patch_sha256)  # enforced here, synchronously, before anything runs
        try:
            runner.submit(sid, orchestrator.continue_session, sid)
        except RuntimeError as exc:
            raise OrchestratorError("session_busy", "This session is already being processed.") from exc
        return session_view(sid)

    @app.post("/api/debug-sessions/{sid}/reject", response_model=SessionView)
    def reject_patch(body: PatchDecisionRequest, sid: str = PathParam(pattern=ID_PATTERN)) -> SessionView:
        orchestrator.reject(sid, body.patch_sha256)
        return session_view(sid)

    @app.post("/api/debug-sessions/{sid}/cancel", response_model=SessionView)
    def cancel_session(sid: str = PathParam(pattern=ID_PATTERN)) -> SessionView:
        orchestrator.cancel(sid)
        return session_view(sid)

    @app.get("/api/debug-sessions/{sid}/events")
    async def session_events(
        request: Request, sid: str = PathParam(pattern=ID_PATTERN),
        follow: bool = Query(True, description="false: replay what exists, then close"),
        after: int = Query(0, ge=0),
        last_event_id: int | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        orchestrator.get(sid)  # 404 before streaming starts

        async def stream():
            cursor = after or last_event_id or 0
            quiet = 0.0
            while True:
                for event in store.steps_after(sid, cursor):
                    cursor = event.seq
                    quiet = 0.0
                    yield f"id: {event.seq}\nevent: step\ndata: {event.model_dump_json()}\n\n"
                state = store.get_session(sid).state
                if state in TERMINAL_STATES or not follow:
                    if not store.steps_after(sid, cursor):  # everything flushed
                        yield f"event: snapshot\ndata: {session_view(sid).model_dump_json()}\n\n"
                        return
                    continue
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.3)
                quiet += 0.3
                if quiet >= 15:
                    quiet = 0.0
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- the web UI (built by `npm run build` in frontend/) ----
    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def unknown_endpoint(rest: str):
        raise ApiError("not_found", "Unknown API endpoint.", 404)

    ui_dir = Path(frontend_dir) if frontend_dir is not None else ROOT / "frontend" / "dist"
    if (ui_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
    else:
        @app.get("/", include_in_schema=False)
        def ui_not_built():
            return JSONResponse({
                "message": "The RepoDoctor API is running, but the web UI has not been built.",
                "build": "cd frontend && npm install && npm run build",
                "or_develop": "cd frontend && npm install && npm run dev  (then open http://localhost:5173)",
                "api_docs": "/docs",
            })

    return app
