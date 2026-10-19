# HTTP API

Run: `python -m repodoctor.api` (serves http://127.0.0.1:8000, interactive docs at `/docs`).
Local use only: there are no accounts, so never expose it to a network.

All errors use one envelope: `{"error": {"code": "...", "message": "..."}}`.

| Method and path | Purpose | Notes |
|---|---|---|
| `GET /api/health` | Docker, sandbox image and AI configuration status | Never calls Bedrock (no cost). `status` is `ok` or `degraded`. |
| `POST /api/repositories` | Upload a repository as a `.zip` (multipart field `file`) | 201. Zip-slip, symlinks, zip bombs and `.git` are handled. 20 MB upload / 100 MB unpacked by default. |
| `POST /api/repositories/sample` | Register the bundled sample repository | 201. `kind: "sample"`. |
| `GET /api/repositories/{id}` | Repository details and file list | |
| `POST /api/debug-sessions` | Create a session `{"repository_id": "..."}` | 201. 429 when too many sessions are active. |
| `GET /api/debug-sessions/{id}` | Current state, pending patch, final report | `ui_state` is the label the UI shows. |
| `POST /api/debug-sessions/{id}/start` | Start debugging in the background | 202 immediately. 503 if no model is configured. |
| `POST /api/debug-sessions/{id}/approve` | Approve ONE patch `{"patch_sha256": "..."}` | 202. 409 `patch_mismatch` for a hash that is not pending. Enforced by the backend. |
| `POST /api/debug-sessions/{id}/reject` | Reject the pending patch | 200. Nothing is ever applied. |
| `POST /api/debug-sessions/{id}/cancel` | Cancel a running or waiting session | 200. |
| `GET /api/debug-sessions/{id}/events` | Server-sent events | `step` events (ordered ids) then a final `snapshot`. `?follow=false` replays and closes. Resume with `?after=N` or `Last-Event-ID`. |

Status codes: 404 unknown id, 409 wrong state, 422 malformed input, 413 too large, 429 too many sessions,
503 model not configured.

## Session states and `ui_state`

`INITIALIZE`/`ANALYZE_REPOSITORY` analyzing, `RUN_TESTS` running tests, `ANALYZE_FAILURE`/`INVESTIGATE`/`FORM_HYPOTHESIS`/`RETRY`
investigating, `GENERATE_PATCH` patch proposed, `WAITING_APPROVAL` awaiting approval, `APPLY_PATCH` applying patch,
`VERIFY` verifying, `SUCCESS` success, `FAILED` failed (or `timeout` for time limits), `REJECTED`/`CANCELLED` cancelled,
`NO_FAILURE` success (nothing to fix).

## Demo mode

`REPODOCTOR_ENGINE=demo` uses a scripted patch for the sample repository only. Every response carries
`engine: {"kind": "demo", "simulated": true}` so the UI must label it as simulated. Investigation still uses
the real tools and the sandbox; only the model's patch is scripted.

## Housekeeping

- Sessions waiting for approval longer than `REPODOCTOR_WAITING_TTL_SECONDS` (default 1800) are cancelled and their
  container is released.
- After a restart, sessions that were mid-run are marked `FAILED` (`server_restarted`); sessions waiting for approval
  survive and can still be approved (a fresh sandbox is built from the stored patch).
- At most `REPODOCTOR_MAX_ACTIVE_SESSIONS` (default 2) sessions can be active, because each may hold a container.

## Serving the UI

If `frontend/dist` exists (built with `npm run build`), the same server serves the web UI at `/`; API routes always win,
and unknown `/api/...` paths return the JSON error envelope. Without a build, `/` returns a JSON note explaining how to build it.

## Concurrency note

Work for one session runs strictly in order on a background thread: an approval that arrives while the worker that produced the
patch is still returning is queued behind it, never refused. Different sessions run in parallel (up to
`REPODOCTOR_MAX_ACTIVE_SESSIONS`).
