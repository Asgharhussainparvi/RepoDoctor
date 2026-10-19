# Security

RepoDoctor runs code from repositories it does not control. This document states what
the sandbox does and, just as importantly, what it does not do.

## Execution boundary

`repodoctor/sandbox/manager.py` is the only module allowed to execute processes
(enforced by `tests/unit/test_execution_boundary.py`). Commands are argv lists; no shell
is ever used, and the frontend cannot submit commands.

## Container controls (per debugging session)

| Control | Setting |
|---|---|
| Network | `--network none` |
| Memory | `--memory 512m`, swap disabled |
| CPU / processes | `--cpus 1`, `--pids-limit 256` |
| Privileges | non-root user (uid 10001), `--cap-drop ALL`, `no-new-privileges` |
| Filesystem | read-only root; `/workspace` and `/tmp` are size-capped tmpfs |
| Host access | no bind mounts, no Docker socket, host environment variables not passed |
| Time | in-container `timeout` plus a host-side backstop that kills the container |
| Output | capped (first half + last half kept) |
| Upload | no symlinks, no `.git`/venv/cache dirs, file-count and byte limits |
| Cleanup | `docker rm -f -v` on exit; `python -m repodoctor.sandbox cleanup` removes strays |

## Honest limitations (hackathon-grade, NOT production-grade)

- Docker containers share the host kernel. A kernel or runtime vulnerability could allow escape.
  Production use would need gVisor, Firecracker or a dedicated VM.
- No seccomp or AppArmor profile beyond Docker's defaults.
- tmpfs usage counts toward the container memory limit; filling it can OOM-kill the container.
- Dependencies must already be in the image (no network at run time). Repositories needing
  other packages need a separate, explicitly networked setup step (not built yet).
- The 137 exit code is ambiguous between OOM kill and a forced timeout kill; the manager uses
  elapsed time to tell them apart.
- Fork-bomb and escape-attempt testing is planned for Phase 9, not yet done.

## Patch policy (enforced before `git apply`)

Patches are validated by `repodoctor/tools/patch.py`. A patch may only **modify existing files**,
must use `a/` and `b/` path prefixes, and must stay small (default: at most 5 files, 200 changed
lines, 100 KB). Rejected outright: file creation, deletion, renames, mode changes, binary patches,
paths containing `..`, absolute paths or `.git`, and **any edit to tests or test configuration**
(`tests/`, `test_*.py`, `conftest.py`, `pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`).
The last rule exists so the agent cannot "fix" a failure by editing the test that reports it.

## Approval is enforced in the backend

`Toolbox.apply_patch` refuses to run unless the SHA-256 of the exact normalised patch was registered
via `register_approval()`, which only the orchestrator calls after a persisted human approval.
Approvals are single-use and bound to the patch contents: approving patch A never allows patch B.
`apply_patch`, `check_patch`, `reset_changes` and `register_approval` must never be exposed to the model.

## Verification integrity: what "verified" does and does not mean

- A run counts as passed only if pytest's exit code is 0 **and** the parsed summary shows no failures
  or errors and at least one passing test. Any disagreement is reported as `unparseable`, never as a pass.
- The repository under test controls its own test run. A malicious repository could print a forged
  summary or exit early. The sandbox protects the host; it does not make the test results tamper-proof.
- Passing tests are evidence, not proof of correctness. A patch that hardcodes the value a test expects
  also "verifies" (see `test_verification_only_proves_the_tests_pass`). Human review of the diff
  before approval is the control for this.
- RepoDoctor runs pytest with `-o addopts=` so a repository's own `addopts` cannot suppress the summary
  line; repositories that depend on `addopts` for correct test runs may behave differently.

## The model is untrusted too

- Repository files, test output and comments are fed to the model, so they can contain prompt-injection text.
  The prompts say to treat them as data, but that is a mitigation, not a guarantee.
- What limits the damage: the model only gets read-only tools; it returns a small edit; RepoDoctor builds the
  diff itself; the patch policy rejects test edits, new/deleted files, renames and path tricks; and a human must
  approve the exact diff before anything is applied. The model cannot approve, apply, reset or declare success.
- The Bedrock API key is read from the environment (`AWS_BEARER_TOKEN_BEDROCK`) or the AWS credential chain. It is
  never written to the repository, the database or the logs. Use an expiry date on long-term keys.
- Model calls leave your machine: file contents and test output are sent to Amazon Bedrock. Do not point RepoDoctor
  at code you are not allowed to send to a cloud model. A `global.` inference profile may route a request to any
  supported region; use a `us.` profile if you need to stay in the US.

## The HTTP API

- Binds to 127.0.0.1 and has no accounts. Anything that can reach the port can use it, so never expose it.
- Rejects unknown `Host` headers (DNS rebinding) and cross-site POSTs (an `Origin` that is not in
  `REPODOCTOR_CORS_ORIGINS`). Command-line clients send no Origin and are unaffected.
- A client can never send a command or a host path: it uploads a zip or picks the bundled sample.
- Uploads: size-limited while reading; extraction rejects `..`, absolute and drive paths, never extracts symlinks,
  skips `.git` and cache directories, caps file count and unpacked size (also while streaming), and maps corrupt
  archives to a clear 400. A failed upload leaves no files behind.
- Approval is checked in the backend before any work is scheduled; the UI button is cosmetic.
- Error responses never include stack traces or internal paths.
