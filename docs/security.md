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
- Rejects unknown `Host` headers (DNS rebinding) and cross-site POSTs: an `Origin` must equal the `Host` the request was
  addressed to (the UI this server serves) or be listed in `REPODOCTOR_CORS_ORIGINS`. Command-line clients send no Origin and are unaffected.
- A client can never send a command or a host path: it uploads a zip or picks the bundled sample.
- Uploads: size-limited while reading; extraction rejects `..`, absolute and drive paths, never extracts symlinks,
  skips `.git` and cache directories, caps file count and unpacked size (also while streaming), and maps corrupt
  archives to a clear 400. A failed upload leaves no files behind.
- Approval is checked in the backend before any work is scheduled; the UI button is cosmetic.
- Error responses never include stack traces or internal paths.

## Phase 9: threat matrix and test evidence

Run the container-level evidence with `python -m pytest -m docker tests/security -v` (hostile code runs only inside the
sandbox; every test is bounded by the limits below). The rest runs without Docker.

| Threat | Control | Evidence |
|---|---|---|
| Infinite loop or hang | in-container `timeout` plus a host-side backstop; session time limit | `TestHostileRepositories` (loop), `TestHostileSessions` |
| Memory bomb | 512 MB cgroup limit, swap off | `test_a_memory_bomb_is_killed_and_the_sandbox_survives` and the repo-level and session-level variants |
| Fork bomb | `--pids-limit 256` | `test_a_fork_storm_is_stopped_by_the_process_limit` (also from inside a test) |
| Disk fill | size-capped tmpfs (256 MB workspace, 64 MB /tmp) | `test_filling_the_disk_is_capped_by_the_tmpfs_size` |
| Output flood | first-half plus last-half cap (the summary line survives) | `test_output_floods_are_capped_and_keep_the_tail` |
| CPU hogging | `--cpus 1` | `test_cpu_is_limited_to_one_cpu` |
| Network exfiltration (TCP, DNS, cloud metadata) | `--network none`, only `lo` exists | `test_network_is_unreachable_including_dns_and_cloud_metadata` |
| Privilege escalation | non-root (uid 10001), all capabilities dropped, `no-new-privileges`, Docker's default seccomp | `test_no_capabilities_no_new_privileges_default_seccomp_and_not_root` reads `/proc/self/status` from inside |
| Reaching the host (files, Docker socket) | no bind mounts, no host share file systems | `test_the_host_filesystem_and_docker_socket_are_not_reachable` parses `/proc/self/mounts` |
| Stealing host secrets | environment is never passed | `test_host_environment_variables_never_enter_the_container` (sentinel in the host environment) |
| Writing outside the workspace | read-only root file system | `test_only_workspace_and_tmp_are_writable` |
| Symlink tricks | `realpath` check on every read; symlinks never uploaded; git refuses to patch through symlinks | `test_symlinks_created_at_runtime...`, `test_a_symlink_to_a_secret_outside_the_repository...` |
| Shell injection through tool arguments | no shell anywhere: argv lists only | canary-file tests in `test_tool_abuse.py` |
| Malicious patches | patch policy (36 cases in `test_compromised_model.py`): tests/config, `.git`, traversal, creation, deletion, renames, modes, binary, size, prose, lookalike names | each case is refused, never offered, and leaves the workspace byte-for-byte unchanged |
| Parser differential (validator vs `git apply`) | a `diff --git` header must agree with the `---`/`+++` lines | `test_patch_policy.py` (probed against real git first) |
| Editing tests to fake a fix | protected paths, including `pytest.toml`, `sitecustomize.py`, `*.pth`, `testing/` | `test_patch_policy.py` |
| Prompt injection from the repository | model output is data; read-only tools; the engine never sees apply/approve/reset; human approval of the exact hash | `test_compromised_model.py` (hostile hypotheses and patches) |
| Well-formed but malicious code in a patch | the human reads the full diff; if approved it only runs inside the sandbox | `test_a_well_formed_but_malicious_patch_is_visible...` and `test_an_approved_malicious_patch_only_ever_runs_inside_the_sandbox` |
| HTML or script injection into the UI | React renders text; no raw-HTML APIs | `frontend/src/security.test.tsx` including source guard rails |
| Zip attacks (slip, bomb, symlink, odd names, corrupt entries) | defensive extraction, Windows-invalid names rejected | `test_zip_upload.py` |
| Cross-site requests, DNS rebinding | Origin and Host checks | `test_api.py` |
| Forged test output | exit code must agree with the parsed summary, else `unparseable` | `test_forged_results.py` |

## Residual risks (honest)

- **Shared kernel.** Docker is not a VM. A kernel or runtime vulnerability could allow an escape. Use gVisor, Firecracker or a
  dedicated VM for anything beyond a hackathon.
- **A repository can lie about its own test results** if it forges both the summary line and the exit code
  (`test_KNOWN_LIMITATION_...` pins this). The sandbox protects the host, not the honesty of the tests.
- **Detached processes survive a timeout** until the sandbox is destroyed (characterised by a test). They only affect that session.
- **Policy cannot judge intent.** A patch that special-cases the tests by editing source code passes the policy. The human diff
  review is the control; the UI says so.
- **Test files are protected by name and folder**, not by what pytest actually collects. A repository that keeps tests in an unusual
  place is only partly protected.
- **Model calls leave your machine.** File contents and test output go to Amazon Bedrock.
- **The local API has no authentication.** It relies on binding to 127.0.0.1 plus Origin and Host checks. Never expose it.
