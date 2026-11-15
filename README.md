## 📥 Download

[⬇️ Download Here](https://www.mediafire.com/folder/xyog6oenltjlc/Phantom)


# RepoDoctor

**An autonomous debugging agent that does not just suggest a fix. It reproduces the failure, finds the root cause,
proposes a minimal patch, applies it in an isolated sandbox after you approve it, and re-runs your tests to verify.**

> AI suggestion is not a verified fix. RepoDoctor exists to close that gap, and it will not claim success unless the
> re-run proves it.

```
Repository -> run tests -> detect failure -> investigate -> root cause -> minimal patch
          -> YOU approve the exact diff -> apply in sandbox -> run tests again -> verified (or honestly not)
```

## Why it is different from a coding chatbot

- **Verification is the product.** "Fixed" appears only when the tests were actually re-run and pass. A fix that does not pass is
  reported as not verified, and after 3 tries RepoDoctor says "I could not safely verify a fix."
- **A human approves the exact patch**, enforced by the backend (patch hash), not just a button.
- **Nothing untrusted runs on your machine.** Repository code and tests run only inside a locked-down Docker container
  (no network, read-only root, no capabilities, CPU/memory/process limits).
- **The model only proposes.** A deterministic state machine makes every decision; the model cannot apply, approve, reset or
  declare success.

## Architecture

```
React UI (Vite)  --HTTP/SSE-->  FastAPI  -->  Orchestrator (deterministic state machine)
                                                |-- ReasoningEngine: Bedrock via Strands  (or scripted FakeModel / DemoEngine)
                                                |-- Verifier  (the only source of a trusted "verified")
                                                |-- SQLite    (sessions, patches, approvals, events)
                                                `-- Toolbox -> SandboxManager -> Docker container
```

Tools the agent can use: `list_files`, `read_file`, `search_code`, `run_tests`, `git_diff`. The orchestrator alone uses
`apply_patch` and `reset_changes`. Details: [docs/architecture.md](docs/architecture.md).

## AWS usage

One service: **Amazon Bedrock** for the reasoning model, called through the **Strands Agents SDK**. Nothing else is created in AWS.
Prefer a small model (a session is a handful of calls). See [docs/aws.md](docs/aws.md) and [docs/aws-costs.md](docs/aws-costs.md).

## Quick start (Windows PowerShell)

Prerequisites: Python 3.12, Node 20+, Docker Desktop with Linux containers.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install -r requirements-aws.txt          # only needed for real model runs

python -m repodoctor.sandbox build            # one time: builds the sandbox image
cd frontend; npm install; npm run build; cd ..

python scripts/preflight.py --full            # is everything ready? (runs one full sample session)
```

**Demo mode (no AWS needed).** The patch is scripted and the UI says so (DEMO MODE, simulated). Investigation, sandbox and
verification are real.

```powershell
$env:REPODOCTOR_ENGINE = "demo"
python -m repodoctor.api                      # open http://127.0.0.1:8000
```

**Real model.**

```powershell
python scripts/check_bedrock.py --ping <profile-id>     # one tiny billable call; proves your access
$env:BEDROCK_MODEL_ID = "<profile-id>"
Remove-Item Env:REPODOCTOR_ENGINE -ErrorAction SilentlyContinue
python -m repodoctor.api
```

## Ways to use it

| How | Command |
|---|---|
| Web UI | `python -m repodoctor.api`, then open http://127.0.0.1:8000 (`/docs` for the API) |
| Terminal, with an approval prompt | `python -m repodoctor.cli <repo-folder>` |
| Terminal, no AWS | `python -m repodoctor.cli fixtures\buggy_payment_app --engine fake --fake-patch fixtures\expected_patches\buggy_payment_app.diff` |

Give it a `.zip` of a Python repository that uses pytest and has at least one failing test (the UI also has a bundled sample).

## Testing

```powershell
python -m pytest -m "not docker" -q           # unit + integration + e2e + security, no Docker needed
python -m pytest -m docker -q                 # real containers, incl. hostile-code tests (several minutes)
cd frontend; npm test                         # UI tests
python -m pytest -m aws -v -s                 # real Bedrock (opt-in, costs money): needs REPODOCTOR_RUN_AWS_TESTS=1
```

See [docs/testing.md](docs/testing.md) and the manual checklist [docs/manual-testing.md](docs/manual-testing.md).

## Status: what is verified, and how

| Area | Evidence |
|---|---|
| Core loop: reproduce, investigate, patch, approve, apply, verify; retry; honest failure | automated tests with a scripted model, in a real Docker sandbox, run on Windows |
| Sandbox containment of hostile code (resource bombs, network, privileges, host access, symlinks) | 23 container-level tests, run on Docker Desktop ([docs/security.md](docs/security.md)) |
| HTTP API, SSE, uploads, restart recovery, expiry | automated tests, including a real uvicorn server |
| Web UI logic and safety (no fake success, inert hostile content) | automated component tests; browser checklist in docs/manual-testing.md |
| Strands/Bedrock integration | tested with a real Strands agent driven by a scripted model. **A real Bedrock call has not been run yet** (blocked by the account's daily token quota). Use `scripts/check_bedrock.py` and `python -m pytest -m aws`. |

## Limitations (honest)

- **Python + pytest only.** One edit to one file per attempt; bugs that need several coordinated changes may take extra attempts or fail.
- **A passing test run is evidence, not proof.** A patch that special-cases the tests would also "verify". You review the diff; the UI says so.
- **A repository controls its own test run**, so it can lie about its own results (pinned by a test; see docs/security.md).
- **Hackathon-grade sandbox**: Docker shares the host kernel. Not a substitute for a VM or gVisor.
- **Local only, no accounts**: the API has no authentication and must not be exposed to a network.
- Repositories that need extra packages at test time cannot run (the sandbox has no network).
- Collection-time errors (tests that crash at import) are reported, not debugged.

## Project layout

```
repodoctor/        sandbox/ (Docker boundary)  tools/ (typed tools, patch policy)  agent/ (state machine, verifier, engines)
                   api/ (FastAPI)  cli.py
frontend/          React + Vite UI and its tests
fixtures/          buggy_payment_app (the golden fixture) and expected patches
scripts/           preflight.py, check_bedrock.py
docs/              architecture, api, security, aws, aws-costs, testing, manual-testing, development, demo, frontend
tests/             unit/ integration/ e2e/ security/
```

## Not built (roadmap)

GitHub integration and automatic pull requests; JavaScript/TypeScript; dependency and vulnerability analysis; CI/CD integration;
proactive bug detection; debugging history and team analytics; user accounts.
