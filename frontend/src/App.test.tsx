import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";
import { ApiError } from "./api";
import { DIFF, FakeApi, SAMPLE, finished, gate, makeSession, okHealth, waitingSession } from "./test/fakeApi";

function setup(api = new FakeApi()) {
  const user = userEvent.setup({ applyAccept: false });
  render(<App api={api} pollMs={0} healthMs={0} />);
  return { api, user };
}

const startButton = () => screen.getByRole("button", { name: /start debugging|starting|run again/i });
async function chooseSample(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Use sample repository" }));
  await screen.findByRole("list", { name: "Repository files" });
}
async function startAndWait(api: FakeApi, user: ReturnType<typeof userEvent.setup>) {
  await chooseSample(user);
  await user.click(startButton());
  api.session = waitingSession();
  api.emitStep(1, "ANALYZE_REPOSITORY", "Repository copied into an isolated sandbox.");
  api.emitStep(2, "RUN_TESTS", "Analyzed repository (10 files).");
  api.emitStep(3, "WAITING_APPROVAL", "Patch proposed (1 file, +1/-1). Waiting for your approval.");
  await screen.findByRole("button", { name: "Approve Fix" });
}

describe("before anything is chosen", () => {
  it("shows an idle state, a ready server and a disabled start button with a reason", async () => {
    setup();
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Idle");
    expect(startButton()).toBeDisabled();
    expect(screen.getByText("Choose a repository first.")).toBeInTheDocument();
    expect(screen.getByText(/No activity yet/)).toBeInTheDocument();
  });

  it("explains setup problems in the server's own words and blocks starting", async () => {
    const api = new FakeApi();
    api.health = okHealth({ status: "degraded", sandbox: { ok: false, message: "Sandbox unavailable. Docker must be running." },
                            ai: { ok: false, message: "AI service unavailable. Check AWS configuration. (BEDROCK_MODEL_ID is not set)" } });
    const { user } = setup(api);
    expect(await screen.findByText("Needs setup")).toBeInTheDocument();
    expect(screen.getByText("Sandbox unavailable. Docker must be running.")).toBeInTheDocument();
    expect(screen.getByText(/AI service unavailable\. Check AWS configuration/)).toBeInTheDocument();
    await chooseSample(user);
    expect(startButton()).toBeDisabled();
    expect(screen.getByText("Fix the setup problems above first.")).toBeInTheDocument();
  });

  it("says so when the server is not running", async () => {
    const api = new FakeApi();
    api.health = new ApiError("network", "Cannot reach the RepoDoctor server. Is it running? (python -m repodoctor.api)", 0);
    setup(api);
    expect(await screen.findByText("Server offline")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("python -m repodoctor.api");
  });

  it("labels demo mode as simulated", async () => {
    const api = new FakeApi();
    api.health = okHealth({ engine: { kind: "demo", model_id: null, simulated: true } });
    setup(api);
    expect(await screen.findByText(/DEMO MODE \(simulated\)/)).toBeInTheDocument();
  });
});

describe("choosing a repository", () => {
  it("shows an uploading state while the sample is prepared, then the file tree", async () => {
    const api = new FakeApi();
    const g = gate();
    api.gates.registerSample = g.promise;
    const { user } = setup(api);
    await user.click(await screen.findByRole("button", { name: "Use sample repository" }));
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Uploading repository");
    expect(screen.getByRole("button", { name: "Preparing..." })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Choose .zip" })).toBeDisabled();
    await act(async () => g.open());
    const tree = await screen.findByRole("list", { name: "Repository files" });
    expect(within(tree).getByText("app/")).toBeInTheDocument();
    expect(within(tree).getByText("payment.py")).toBeInTheDocument();
    expect(screen.getByText(/buggy_payment_app/)).toBeInTheDocument();
    expect(startButton()).toBeEnabled();
  });

  it("uploads a zip, and refuses anything else without calling the server", async () => {
    const { api, user } = setup();
    const input = await screen.findByTestId("file-input");
    await user.upload(input, new File(["x"], "notes.txt"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Please choose a .zip file");
    expect(api.calls.some((c) => c.startsWith("upload:"))).toBe(false);
    await user.upload(input, new File(["x"], "my-repo.zip"));
    await waitFor(() => expect(api.calls).toContain("upload:my-repo.zip"));
    expect(await screen.findByRole("list", { name: "Repository files" })).toBeInTheDocument();
  });

  it("shows a server error from an upload and lets the user dismiss it", async () => {
    const api = new FakeApi();
    api.fail["upload:bad.zip"] = new ApiError("invalid_zip", "That file is not a valid zip archive.", 400);
    const { user } = setup(api);
    await user.upload(await screen.findByTestId("file-input"), new File(["x"], "bad.zip"));
    expect(await screen.findByRole("alert")).toHaveTextContent("That file is not a valid zip archive.");
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("That file is not a valid zip archive.")).not.toBeInTheDocument();
  });
});

describe("the happy path", () => {
  it("shows live progress, requires approval, locks the buttons while applying, then confirms", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await startAndWait(api, user);

    expect(api.calls).toEqual(expect.arrayContaining(["createSession", "startSession"]));
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Awaiting your approval");
    const timeline = screen.getByRole("list", { name: "Debugging progress" });
    expect(within(timeline).getByText("Analyzed repository (10 files).")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Proposed Fix" })).toBeInTheDocument();
    const diff = screen.getByRole("region", { name: "Proposed code change" });
    expect(within(diff).getByText("return self._gateway.charge(user_id, amount_cents)", { exact: false })).toBeInTheDocument();
    expect(within(diff).getByText("return self._gateway.charge(customer_id, amount_cents)", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/Nothing is applied until you approve/)).toBeInTheDocument();
    expect(screen.queryByTestId("outcome")).not.toBeInTheDocument();
    expect(screen.queryByText(/CONFIRMED/)).not.toBeInTheDocument();

    const g = gate();
    api.gates[`approve:${"c".repeat(4)}`] = g.promise;
    await user.click(screen.getByRole("button", { name: "Approve Fix" }));
    expect(screen.getByRole("button", { name: "Approving..." })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();

    api.session = finished("SUCCESS", "success", {}, true);
    await act(async () => g.open());
    const outcome = await screen.findByTestId("outcome");
    expect(outcome).toHaveTextContent("DEBUGGING COMPLETE");
    expect(outcome).toHaveTextContent("CONFIRMED");
    expect(screen.getByTestId("before")).toHaveTextContent("1 failed / 31 passed");
    expect(screen.getByTestId("after")).toHaveTextContent("0 failed / 32 passed ✓");
    expect(screen.getByTestId("verdict")).toHaveTextContent("CONFIRMED ✓");
    expect(screen.getByRole("heading", { name: "Applied Fix" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve Fix" })).not.toBeInTheDocument();
    expect(startButton()).toHaveTextContent("Run again");
  });

  it("sends the hash of exactly the patch that is on screen", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await startAndWait(api, user);
    api.session = finished("SUCCESS", "success", {}, true);
    await user.click(screen.getByRole("button", { name: "Approve Fix" }));
    await waitFor(() => expect(api.calls).toContain("approve:cccc"));
  });

  it("keeps the session id in the address so a refresh reattaches", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await chooseSample(user);
    await user.click(startButton());
    expect(window.location.hash).toBe("#s=bbbbbbbbbbbb");
  });
});

describe("never claiming more than was verified", () => {
  it("shows a rejected patch as cancelled with nothing applied", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await startAndWait(api, user);
    api.session = finished("REJECTED", "cancelled");
    api.session.report!.verification = null;
    api.session.report!.after = null;
    await user.click(screen.getByRole("button", { name: "Reject" }));
    const outcome = await screen.findByTestId("outcome");
    expect(outcome).toHaveTextContent("Patch rejected");
    expect(outcome).toHaveTextContent("No changes were applied.");
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Cancelled");
    expect(screen.queryByText(/DEBUGGING COMPLETE|CONFIRMED ✓/)).not.toBeInTheDocument();
    expect(screen.getByTestId("verdict")).toHaveTextContent("not performed");
    expect(screen.getByTestId("after")).toHaveTextContent("not run");
  });

  it("shows a failed verification honestly, with what did not work", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await startAndWait(api, user);
    api.session = finished("FAILED", "failed", { message: "I could not safely verify a fix.", error_code: "attempts_exhausted" });
    api.session.report!.attempt_feedback = [{ attempt: 1, patch_sha256: null, reason_code: "tests_still_failing", message: "The proposed fix did not pass verification. RepoDoctor will not claim success." }];
    await user.click(screen.getByRole("button", { name: "Approve Fix" }));
    const outcome = await screen.findByTestId("outcome");
    expect(outcome).toHaveTextContent("I could not safely verify a fix.");
    expect(outcome).toHaveTextContent("attempts_exhausted");
    expect(screen.queryByText(/DEBUGGING COMPLETE/)).not.toBeInTheDocument();
    expect(screen.getByTestId("verdict")).toHaveTextContent("NOT CONFIRMED (tests_still_failing)");
    expect(screen.getByTestId("after").textContent).not.toContain("✓");
    expect(screen.getByText(/tests_still_failing/, { selector: "code" })).toBeInTheDocument();
  });

  it("does not trust a SUCCESS state that lacks a confirmed verification", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await startAndWait(api, user);
    api.session = finished("SUCCESS", "success", {}, false);
    await user.click(screen.getByRole("button", { name: "Approve Fix" }));
    const outcome = await screen.findByTestId("outcome");
    expect(outcome).toHaveTextContent("Result could not be verified");
    expect(screen.queryByText(/DEBUGGING COMPLETE/)).not.toBeInTheDocument();
    expect(screen.getByTestId("verdict")).toHaveTextContent("NOT CONFIRMED");
  });

  it("reports a time limit as a timeout", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await chooseSample(user);
    await user.click(startButton());
    api.session = finished("FAILED", "timeout", { message: "Test execution exceeded the allowed time.", error_code: "test_timeout" });
    api.session.report!.before = null;
    api.emitStep(1, "FAILED", "Test execution exceeded the allowed time.", "error");
    expect(await screen.findByTestId("outcome")).toHaveTextContent("Test execution exceeded the allowed time.");
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Timed out");
  });

  it("explains that a passing repository has nothing to fix", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await chooseSample(user);
    await user.click(startButton());
    api.session = finished("NO_FAILURE", "success", { message: "All tests pass. There is no failure to debug." });
    api.session.report!.before = null;
    api.emitStep(1, "NO_FAILURE", "All tests pass. There is no failure to debug.");
    const outcome = await screen.findByTestId("outcome");
    expect(outcome).toHaveTextContent("Nothing to fix");
    expect(outcome).not.toHaveTextContent("DEBUGGING COMPLETE");
  });
});

describe("errors and controls", () => {
  it("shows a failed start, re-enables the button and retries without creating a second session", async () => {
    const api = new FakeApi();
    api.fail.startSession = new ApiError("ai_unavailable", "AI service unavailable. Check AWS configuration. (BEDROCK_MODEL_ID is not set)", 503);
    const { user } = setup(api);
    await chooseSample(user);
    await user.click(startButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("AI service unavailable. Check AWS configuration.");
    await waitFor(() => expect(startButton()).toBeEnabled());
    api.fail.startSession = undefined;
    await user.click(startButton());
    await waitFor(() => expect(api.calls.filter((c) => c === "startSession")).toHaveLength(2));
    expect(api.calls.filter((c) => c === "createSession")).toHaveLength(1);
  });

  it("disables start while the request is in flight and shows the analyzing state", async () => {
    const api = new FakeApi();
    const g = gate();
    api.gates.startSession = g.promise;
    const { user } = setup(api);
    await chooseSample(user);
    await user.click(startButton());
    expect(startButton()).toBeDisabled();
    expect(startButton()).toHaveTextContent("Starting...");
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Analyzing repository");
    await act(async () => g.open());
  });

  it("can cancel a running session", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await chooseSample(user);
    await user.click(startButton());
    api.session = makeSession({ state: "INVESTIGATE", ui_state: "investigating" });
    api.emitStep(1, "INVESTIGATE", "Located failing test: t");
    await screen.findByRole("button", { name: "Cancel" });
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Investigating");
    api.session = finished("CANCELLED", "cancelled");
    api.session.report!.verification = null;
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByTestId("outcome")).toHaveTextContent("Session cancelled");
  });

  it("locks repository changes while a session is running", async () => {
    const api = new FakeApi();
    const { user } = setup(api);
    await chooseSample(user);
    await user.click(startButton());
    api.session = makeSession({ state: "RUN_TESTS", ui_state: "running tests" });
    api.emitStep(1, "RUN_TESTS", "Analyzed repository (10 files).");
    await screen.findByRole("button", { name: "Cancel" });
    expect(screen.getByRole("button", { name: "Use sample repository" })).toBeDisabled();
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Running tests");
  });

  it("reattaches to a session after a page refresh", async () => {
    window.location.hash = "#s=bbbbbbbbbbbb";
    const api = new FakeApi();
    api.session = waitingSession();
    setup(api);
    expect(await screen.findByRole("button", { name: "Approve Fix" })).toBeInTheDocument();
    expect(await screen.findByRole("list", { name: "Repository files" })).toBeInTheDocument();
    expect(api.handlers).not.toBeNull(); // it re-subscribed, so the timeline replays
  });

  it("falls back to a clean idle page when the remembered session no longer exists", async () => {
    window.location.hash = "#s=bbbbbbbbbbbb";
    const api = new FakeApi();
    api.fail.getSession = new ApiError("session_not_found", "No session.", 404);
    setup(api);
    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(screen.getByLabelText("Session state")).toHaveTextContent("Idle");
  });

  it("shows the sample repository diff exactly as the server sent it", async () => {
    const api = new FakeApi();
    api.session = waitingSession();
    window.location.hash = "#s=bbbbbbbbbbbb";
    setup(api);
    const diff = await screen.findByRole("region", { name: "Proposed code change" });
    expect(diff.textContent).toContain("customer_id");
    expect(DIFF).toContain("customer_id");
    expect(SAMPLE.files.length).toBeGreaterThan(0);
  });
});
