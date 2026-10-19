# Development guide

## Setup

```powershell
python -m venv .venv; .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt -r requirements-aws.txt
python -m repodoctor.sandbox build              # sandbox image (re-run if docker/sandbox.Dockerfile changes)
cd frontend; npm install; cd ..
python scripts/preflight.py                     # what is missing?
```

Python 3.12 is the tested version. Python 3.10.0 specifically does not work (a pydantic incompatibility); 3.10.1+ is the minimum.

## Layout and rules

| Path | Rule |
|---|---|
| `repodoctor/sandbox/manager.py` | The ONLY module that may execute processes (enforced by `tests/unit/test_execution_boundary.py`). Argv lists only, never a shell. |
| `repodoctor/tools/` | Typed tools. They never raise into the agent loop; every result has `ok`, `error_code`, `error`. Patch policy lives in `patch.py` and `paths.py`. |
| `repodoctor/agent/orchestrator.py` | The only code that changes session state, through one transition table (`states.py`). |
| `repodoctor/agent/verifier.py` | The only way to produce a trusted `verified=True` (sealed result). |
| `repodoctor/agent/bedrock_engine.py` | Strands/Bedrock adapter. Strands and boto3 are imported lazily so the core works without them. |
| `repodoctor/api/` | Thin. Validation, scheduling, SSE. No business decisions. |
| `fixtures/` | Golden fixtures. Tests find the bug line by content, and formatters are told to leave `fixtures/` alone (`pyproject.toml`). |

## Conventions

- Type hints everywhere; small modules; no secrets in code (`.env.example` documents the variables; RepoDoctor reads the process environment).
- Format with `ruff format .` (it skips `fixtures/`).
- Tests assert structured outcomes, never exact model wording.
- Commit in small steps, for example `feat:`, `fix:`, `test:`, `docs:`. Run `python -m pytest -m "not docker" -q` before committing.
- Windows notes: the suite spawns many `git` and `pytest` processes, so the full run takes several minutes. Use `python -m pytest tests/unit -q` as the fast loop.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `REPODOCTOR_ENGINE` | `bedrock` | `bedrock` (real model) or `demo` (scripted patch, sample repository only) |
| `BEDROCK_MODEL_ID` | none | Inference profile id for Bedrock |
| `AWS_REGION` | `us-east-1` | Bedrock region |
| `AWS_BEARER_TOKEN_BEDROCK` | none | Bedrock API key (or use normal AWS credentials) |
| `REPODOCTOR_MODEL_MAX_TOKENS` | 2048 | Output cap per model response |
| `REPODOCTOR_MAX_SESSION_TOKENS` | 200000 | Token budget per session |
| `REPODOCTOR_PRICE_INPUT_PER_MTOK`, `REPODOCTOR_PRICE_OUTPUT_PER_MTOK` | none | Show a cost estimate (USD per 1M tokens) |
| `REPODOCTOR_SANDBOX_IMAGE`, `_MEMORY`, `_CPUS` | `repodoctor-sandbox:0.1`, 512m, 1 | Container image and limits |
| `REPODOCTOR_TEST_TIMEOUT_SECONDS` | 60 | Per test run |
| `REPODOCTOR_MAX_OUTPUT_BYTES` | 1000000 | Output cap per command |
| `REPODOCTOR_DATA_DIR`, `_HOST`, `_PORT` | `data`, `127.0.0.1`, 8000 | API server |
| `REPODOCTOR_MAX_ACTIVE_SESSIONS` | 2 | Concurrent sessions (each may hold a container) |
| `REPODOCTOR_WAITING_TTL_SECONDS` | 1800 | Cancel sessions waiting for approval this long |
| `REPODOCTOR_DEMO_PATCH` | none | Demo mode: use a different scripted patch (for example the wrong one) |
| `REPODOCTOR_RUN_AWS_TESTS` | none | `1` enables the opt-in real-Bedrock tests |

## Extending

- **A new engine:** implement `ReasoningEngine` (`investigate`, `propose_patch`), receive only `AgentTools`, return structured models.
  It never needs (and never gets) apply/approve/reset.
- **A new tool:** add typed input/output models, a handler in `Toolbox._invoke`, tests with `LocalSandbox`, and decide whether it counts
  against the agent's tool budget. Anything that mutates state is orchestrator-only.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ForwardRef.__init__() got an unexpected keyword argument 'is_class'` | Python 3.10.0. Use Python 3.12. |
| "Sandbox unavailable. Docker must be running." | Start Docker Desktop; check `docker info`; Linux containers. |
| "Sandbox image ... not found" | `python -m repodoctor.sandbox build` |
| Containers left over | `python -m repodoctor.sandbox cleanup` |
| `ThrottlingException: Too many tokens per day` | Your Bedrock daily token quota for that model is probably 0. See docs/aws.md. |
| A test fails only after your editor formatted a fixture | Tests locate the bug by content; restore with `git checkout -- fixtures`. |
