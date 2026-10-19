# Testing

## The pyramid

| Level | Where | Needs | Runs by default? |
|---|---|---|---|
| Unit | `tests/unit` | nothing | yes |
| Integration (agent, tools, API, engine with a real Strands agent and a scripted model) | `tests/integration` | nothing (git and pytest on the host, trusted fixture only) | yes |
| End to end (full flows through the API, a real uvicorn server) | `tests/e2e` | nothing | yes |
| Security (compromised model, hostile tool arguments, forged results) | `tests/security` | nothing | yes |
| Container tests (real sandbox, hostile code inside it) | marked `docker` | Docker + the sandbox image | skipped if Docker is down; FAIL if Docker is up but the image is missing |
| Real Bedrock | marked `aws` | `REPODOCTOR_RUN_AWS_TESTS=1`, `BEDROCK_MODEL_ID`, credentials with quota | skipped unless enabled |
| Frontend | `frontend/src/*.test.ts(x)` | Node | `npm test` |

`LocalSandbox` (in `tests/conftest.py`) is a test double with the same interface as `SandboxManager` that runs on the host. It is used only
with the trusted fixture; anything hostile runs only in real containers.

## Commands

```powershell
python -m pytest tests/unit -q                       # fast loop
python -m pytest -m "not docker" -q                  # everything that needs no Docker
python -m pytest -m docker -q                        # all container tests
python -m pytest -m docker tests/security -v         # the hostile-code container tests
python -m pytest -m aws -v -s                        # real Bedrock (costs money)
cd frontend; npm test; npm run typecheck             # UI
python scripts/preflight.py --full                   # demo readiness, incl. one real sample session
```

## What is deliberately not automated

Visual polish, animation, responsive layout, and the real browser (see [manual-testing.md](manual-testing.md)); Docker Desktop being stopped
(manual #11); a real Bedrock model's judgement (the `aws` tests check structure, never wording); repeated demo reliability (record only after
three clean runs).

## Rules the tests follow

- Assert structured outcomes (`state`, `error_code`, counts), never model wording.
- No test may pass because a result was faked: success is asserted only together with a sealed, verified result.
- Platform-dependent behaviour is tested for both: Windows line endings, clock resolution and file ordering have each caused (and now have tests for) real bugs.

## Final quality bar

| Area | Item | Status |
|---|---|---|
| Functionality | repository accepted (zip or sample), tests run, failures parsed, agent investigates, root cause, patch, approval required, patch applied, tests re-run, success verified | automated + manual checklist |
| Safety | code runs only in the sandbox; timeout; resource limits; arbitrary host execution prevented; approval enforced server-side | automated, 23 container tests passed on Docker Desktop |
| AI | uses tools; bounded iterations; structured outputs; no fake success; model failures handled | automated with a scripted model |
| AI | **a real Bedrock model completes a session** | **pending: needs working quota (docs/aws.md)** |
| AWS | integration documented; credentials externalised; costs documented; no unnecessary resources | done (docs/aws.md, docs/aws-costs.md); real call pending |
| UI | loading states, errors, diff, approval, timeline, verification | automated + manual checklist |
| Testing | unit, integration, deterministic fixture, e2e happy path, manual checklist | done |
| Documentation | README, architecture, testing, security, AWS, cost, demo script | done |
