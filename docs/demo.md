# Demo script

**One story:** a repository has 31 passing tests and 1 failing test. RepoDoctor reproduces it, finds the root cause, proposes a one-line fix,
you approve it, and it proves the fix by re-running the tests: 32 passed.

## Before recording (10 minutes)

```powershell
python scripts/preflight.py --full        # must end with READY and a verified full demo run
$env:REPODOCTOR_ENGINE = "demo"           # or: a working BEDROCK_MODEL_ID (see docs/aws.md)
python -m repodoctor.api                  # open http://127.0.0.1:8000
```

- Do the whole flow once, then reload the page. Record only after three clean runs.
- Browser at 100 to 125% zoom, bookmarks hidden, light mode, window about 1280 px wide.
- `docker ps -a --filter label=repodoctor.managed=true` should be empty. If not: `python -m repodoctor.sandbox cleanup`.
- **If you use demo mode, say so out loud.** The page shows a DEMO MODE (simulated) banner; do not hide it. Real: tests, sandbox, verification.

## The 3-minute story

| Time | Do | Say |
|---|---|---|
| 0:00 | Open the page | "AI coding tools suggest fixes. Nobody tells you whether they work. RepoDoctor proves it." |
| 0:15 | Click **Use sample repository** | "A small payment app. 32 tests, and one of them fails." |
| 0:30 | Click **Start debugging**; watch the timeline | "It runs the tests in an isolated container and reproduces the failure: 1 failed, 31 passed. Then it investigates with read-only tools and finds the root cause: `user_id` is used but never defined." |
| 1:10 | Show the **Proposed Fix** diff | "One line. Nothing has been applied. I decide. Note the warning: passing tests are evidence, not proof, so I read the change." |
| 1:40 | Click **Approve Fix** | "Approval is enforced by the backend, tied to this exact patch." |
| 1:50 | Verification appears | "It applied the patch in the sandbox and re-ran the tests: before 1 failed / 31 passed, after 0 failed / 32 passed. Verification: confirmed." |
| 2:10 | Mention safety | "Everything ran in a locked-down container: no network, read-only filesystem, no privileges, CPU and memory limits. We tested hostile repositories, fork bombs and memory bombs against it." |
| 2:30 | **Honesty beat** (optional, prepared earlier): the wrong-patch run | "If a fix does not pass, RepoDoctor says so. It tries again, up to three times, then reports: *I could not safely verify a fix.* It never fakes success." |
| 2:50 | Close | "Built on Amazon Bedrock with Strands. Suggestion is not a verified fix; verification is." |

### Preparing the honesty beat

```powershell
$env:REPODOCTOR_ENGINE = "demo"
$env:REPODOCTOR_DEMO_PATCH = "fixtures\expected_patches\wrong_patch.diff"
python -m repodoctor.api            # run the same flow; approve the wrong patch
```

Restart without `REPODOCTOR_DEMO_PATCH` for the main take.

## If something goes wrong on camera

| Problem | Recovery |
|---|---|
| Docker stopped | The header shows "Needs setup" with the reason; start Docker, wait 10 s, continue |
| AI service unavailable | That is the real message. Switch to demo mode and say so, or show it as part of the error handling story |
| A browser glitch | Use the terminal: `python -m repodoctor.cli fixtures\buggy_payment_app --engine fake --fake-patch fixtures\expected_patches\buggy_payment_app.diff` |
| Containers left over | `python -m repodoctor.sandbox cleanup` |

## What not to claim

- Do not say a fix is "correct", only "verified by the project's tests".
- Do not describe the sandbox as production-grade. It is hackathon-grade (see docs/security.md).
- Do not present a demo-mode run as a model-written fix.
- Cost: quote the measured numbers from the log (`model_call` lines) or the CLI's usage line, not the estimate.
