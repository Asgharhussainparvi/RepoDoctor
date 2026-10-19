# Frontend

React 19 + Vite 7 + TypeScript. No state library, no CSS framework, no browser storage.

## Run it

Needs Node 20+ (you have 24).

```powershell
cd frontend
npm install

# A) One process: build the UI, let the Python server serve it
npm run build
cd ..
python -m repodoctor.api          # open http://127.0.0.1:8000

# B) Two processes for UI development (hot reload; /api is proxied to :8000)
python -m repodoctor.api          # terminal 1
cd frontend; npm run dev          # terminal 2, open http://localhost:5173
```

Try it without AWS: `$env:REPODOCTOR_ENGINE = "demo"` before starting the server (the page then shows a "DEMO MODE (simulated)" banner).

## Test it

```powershell
cd frontend
npm test            # 47 tests, a few seconds
npm run typecheck
```

The tests cover logic and behaviour, not visuals: diff parsing and line numbers, timeline derivation, API error mapping,
the rule that success is only shown for a confirmed verification, and full UI flows against a fake server (happy path,
reject, failed verification, timeout, nothing-to-fix, API errors, cancel, refresh/reattach, disabled buttons while a request runs).

## How it is organised

| File | Role |
|---|---|
| `src/api.ts` | The only code that talks to the server. Maps the error envelope to `ApiError`; wraps SSE. Replaced by a fake in tests. |
| `src/useWorkflow.ts` | All workflow state (health, repository, session, events, pending action, error) and the actions. |
| `src/uiState.ts` | The 13 UI states and `isVerifiedSuccess`, the single place that decides whether the UI may say a bug was fixed. |
| `src/timeline.ts`, `src/diff.ts` | Pure functions: events to timeline rows; unified diff to numbered rows. |
| `src/components/*` | Header, banners, repository panel, timeline, patch panel (diff + Approve/Reject), verification panel. |

## Design rules

- It is a workflow, not a chat: repository, timeline, proposed fix, verification.
- Success is displayed only when the server reports `SUCCESS` AND a verification that is `verified` with zero failing tests.
  A `SUCCESS` without that shows "Result could not be verified". Rejected, cancelled and failed sessions never show a check mark.
- The patch panel says plainly that passing tests are not proof the fix is correct, and nothing is applied until you approve.
- Buttons are disabled while their request is in flight; every API error is shown in plain words; nothing leaves a blank screen.
- The session id is kept in the address (`#s=<id>`), so a refresh reattaches and the timeline replays.
- The timeline shows high-level progress only, never model reasoning.
- Server-provided messages (Docker down, AI not configured) are shown as written.
