# Manual testing checklist

Run the automated suites first (`python -m pytest -m "not docker" -q`, `python -m pytest -m docker -q`, `cd frontend; npm test`).
These checks cover what automation cannot: the real browser, the real Docker daemon and real AWS.

Start the server and UI: `cd frontend; npm install; npm run build; cd ..; python -m repodoctor.api` and open http://127.0.0.1:8000.
For checks that do not need AWS set `$env:REPODOCTOR_ENGINE = "demo"` first. Restart the server after changing environment variables.

| # | Check | Steps | Expected |
|---|---|---|---|
| 1 | Header and idle state | Open the page | "Ready" pill, state badge "Idle", Start disabled with "Choose a repository first." |
| 2 | Happy path (demo) | Use sample repository, Start debugging, wait for the diff, Approve Fix | Timeline fills live; diff shows `-...user_id` / `+...customer_id`; after approval: "DEBUGGING COMPLETE", Before 1 failed / 31 passed, After 0 failed / 32 passed with a check mark, Verification CONFIRMED. A "DEMO MODE (simulated)" banner is visible throughout. |
| 3 | Reject path | Repeat 2 but click Reject | Banner "Patch rejected. No changes were applied."; no check marks; Verification "not performed". |
| 4 | Verification failure | Stop the server, `$env:REPODOCTOR_DEMO_PATCH = "fixtures\expected_patches\wrong_patch.diff"`, restart, run 2 and approve | Banner "I could not safely verify a fix."; Verification "NOT CONFIRMED (tests_still_failing)"; no check marks; "What did not work" lists the attempts. Never "DEBUGGING COMPLETE". |
| 5 | Cancel | Start, click Cancel while it runs (or while awaiting approval) | State "Cancelled"; `docker ps -a --filter label=repodoctor.managed=true` shows no container afterwards. |
| 6 | Refresh mid-session | During or after step 2 press F5 | Same session reappears with its full timeline (the address contains `#s=...`). |
| 7 | Slow tests (timeout) | Build a zip of the fixture plus a test that sleeps 120 s. Set `$env:REPODOCTOR_TEST_TIMEOUT_SECONDS = "10"`, restart, upload it, Start | Banner "Test execution exceeded the allowed time."; state badge "Timed out". |
| 8 | Empty archive | Upload a zip with no files | Error banner "The archive contains no files." |
| 9 | Not a zip | Upload a .txt, then a renamed non-zip .zip | "Please choose a .zip file..." then "That file is not a valid zip archive." |
| 10 | Invalid project | Upload a zip containing only README.md, Start | "This does not look like a Python project: no .py files found." |
| 11 | Docker unavailable | Quit Docker Desktop, reload | "Needs setup" pill; notice "Sandbox unavailable. Docker must be running."; Start disabled. Start Docker again: the pill returns to Ready within ~10 s. |
| 12 | AWS not configured | Unset `REPODOCTOR_ENGINE`, clear `BEDROCK_MODEL_ID`, restart | "Needs setup"; notice starts "AI service unavailable. Check AWS configuration."; Start disabled. |
| 13 | AWS denied | Set `BEDROCK_MODEL_ID` to a model your account cannot call, restart, run the sample | Session fails with "AI service unavailable. Check AWS configuration."; no crash, no fake success. |
| 14 | Real model (when quota allows) | Set `BEDROCK_MODEL_ID` to a working profile, run the sample | A real hypothesis and patch; approve; verified success. Note tokens and cost from the server log (`model_call` lines). |
| 15 | Server offline | Stop the server, reload or wait 10 s | "Server offline" pill with "python -m repodoctor.api" in the message. |
| 16 | Buttons while busy | Click Approve Fix and watch | Buttons show "Approving..." and are disabled until the response. |
| 17 | Diff readability | Look at the diff | Old and new line numbers, green additions, red deletions, scrolls inside its box for long diffs. |
| 18 | Responsive layout | Narrow the window below 760 px | Panels stack in one column; nothing overflows horizontally. |
| 19 | Keyboard and focus | Tab through the page | Every button is reachable with a visible focus ring; Approve and Reject are reachable. |
| 20 | Dark mode | Switch the OS to dark mode | Colours adapt; diff stays readable. |
| 21 | Cleanup | After all runs, `docker ps -a --filter label=repodoctor.managed=true` | Empty. If not: `python -m repodoctor.sandbox cleanup`. |
