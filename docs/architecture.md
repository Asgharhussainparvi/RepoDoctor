# Architecture (through Phase 4)

```
Orchestrator (deterministic state machine, owns every decision)
  |-- ReasoningEngine  (FakeModel now; Bedrock + Strands in Phase 5)  -- proposes only
  |-- Verifier         (the only producer of a sealed "verified" result)
  |-- SessionStore     (SQLite: sessions, steps, tool calls, patches, verification)
  `-- Toolbox -> SandboxManager -> Docker container   (the only execution boundary)
```

## States

```
INITIALIZE -> ANALYZE_REPOSITORY -> RUN_TESTS -> ANALYZE_FAILURE -> INVESTIGATE -> FORM_HYPOTHESIS
   -> GENERATE_PATCH -> WAITING_APPROVAL -> APPLY_PATCH -> VERIFY -> SUCCESS
                              |                              `-> RETRY -> GENERATE_PATCH
                              `-> REJECTED
Any active state -> FAILED | CANCELLED.   RUN_TESTS -> NO_FAILURE when the tests already pass.
Terminal: SUCCESS, FAILED, REJECTED, CANCELLED, NO_FAILURE.
```

## Invariants (each one has a test)

1. Only the orchestrator changes state; every change goes through one allowed-transition table.
2. `APPLY_PATCH` is reachable only from `WAITING_APPROVAL`; `SUCCESS` only from `VERIFY`.
   Removing `WAITING_APPROVAL` from the graph makes both unreachable.
3. `WAITING_APPROVAL` is persisted in SQLite and survives a restart. Approval is one atomic
   transaction bound to the SHA-256 of the exact patch; the applied diff is read from the database,
   never from the caller. The Toolbox re-checks the same hash (defence in depth).
4. `SUCCESS` requires a `VerificationResult` that is sealed by the Verifier (HMAC with a per-process
   secret), has `verified=True`, and is unmodified. Forged or edited results are refused.
5. The engine receives only five inspection tools (`AgentTools`); it never sees apply/reset/approval.
6. Every patch the engine is asked for counts as an attempt (default maximum 3), whether it is valid,
   invalid, a duplicate or empty. After the last one: "I could not safely verify a fix."
7. A rejected patch ends the session (`REJECTED`) and is never applied.
8. The sandbox is destroyed on every terminal state. It stays alive while waiting for approval.

## Limits

| Limit | Default | Where |
|---|---|---|
| Patch attempts | 3 | `AgentConfig.max_patch_attempts` |
| Agent tool calls | 25 (orchestrator's own calls excluded) | `ToolLimits.max_tool_calls` |
| Active session time | 900 s (waiting for a human is excluded) | `AgentConfig.max_active_seconds` |
| Test run | sandbox default 60 s | `AgentConfig.test_timeout_seconds` |

## Not built yet

Real model (Phase 5), HTTP API and SSE (Phase 6), UI (Phase 7). A repository whose tests fail at
collection time (exit code 2) is reported as "could not run the tests" rather than debugged.
A session left waiting for approval keeps its container until it is approved, rejected or cancelled
(`python -m repodoctor.sandbox cleanup` removes strays).
