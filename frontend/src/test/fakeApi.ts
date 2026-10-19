import { act } from "@testing-library/react";
import { type Api, ApiError, type EventHandlers } from "../api";
import type { AgentEvent, Counts, Health, Repository, Session, SessionState, UiState, Verification } from "../types";

export const counts = (passed: number, failed: number): Counts => ({
  passed, failed, errors: 0, skipped: 0, xfailed: 0, xpassed: 0, deselected: 0, warnings: 0,
});

export const DIFF = [
  "--- a/app/payment.py", "+++ b/app/payment.py", "@@ -19,7 +19,7 @@",
  '         """Charge a customer."""', "         validate_amount_cents(amount_cents)",
  "-        return self._gateway.charge(user_id, amount_cents)",
  "+        return self._gateway.charge(customer_id, amount_cents)", "",
].join("\n");

export const okHealth = (over: Partial<Health> = {}): Health => ({
  status: "ok", version: "0.1.0", sandbox: { ok: true, message: "Docker is running." },
  ai: { ok: true, message: "Configured." }, engine: { kind: "custom", model_id: null, simulated: false },
  active_sessions: 0, max_active_sessions: 2, ...over,
});

export const SAMPLE: Repository = {
  id: "aaaaaaaaaaaa", name: "buggy_payment_app (sample)", kind: "sample", file_count: 3, total_bytes: 100,
  created_at: "now", files: ["app/payment.py", "tests/test_payment.py", "README.md"],
};

export function makeSession(over: Partial<Session> = {}): Session {
  return {
    session_id: "bbbbbbbbbbbb", state: "INITIALIZE", ui_state: "analyzing", message: "", error_code: null,
    patch_attempts: 0, pending_patch: null, report: null, repository_id: SAMPLE.id,
    engine: { kind: "custom", model_id: null, simulated: false }, ...over,
  };
}

export const waitingSession = (): Session => makeSession({
  state: "WAITING_APPROVAL", ui_state: "awaiting approval", patch_attempts: 1,
  pending_patch: { sha256: "c".repeat(64), diff: DIFF, summary: "Use customer_id.", files: ["app/payment.py"], attempt: 1, status: "proposed" },
});

export function verification(verified: boolean): Verification {
  return {
    verified, reason_code: verified ? "verified" : "tests_still_failing",
    message: verified ? "Verification passed." : "The proposed fix did not pass verification. RepoDoctor will not claim success.",
    before: counts(31, 1), after: verified ? counts(32, 0) : counts(31, 1),
    previously_failing: ["tests/test_payment.py::t"], still_failing: verified ? [] : ["tests/test_payment.py::t"],
    new_failures: [], files_changed: ["app/payment.py"], patch_sha256: "c".repeat(64),
  };
}

export function finished(state: SessionState, ui: UiState, over: Partial<Session> = {}, verified = false): Session {
  return makeSession({
    state, ui_state: ui, patch_attempts: 1,
    report: {
      session_id: "bbbbbbbbbbbb", outcome: state, headline: "h", message: "m",
      root_cause: { file: "app/payment.py", line: 22, description: "user_id is undefined." },
      before: counts(31, 1), after: state === "SUCCESS" ? counts(32, 0) : null,
      failing_tests_before: ["tests/test_payment.py::t"], failing_tests_after: [],
      files_changed: ["app/payment.py"], patch_diff: DIFF, patch_sha256: "c".repeat(64),
      verification: state === "SUCCESS" || state === "FAILED" ? verification(verified) : null,
      patch_attempts: 1, attempt_feedback: [],
    },
    ...over,
  });
}

/** A scriptable stand-in for the server. Tests change `session`, then call emitStep to trigger a refresh. */
export class FakeApi implements Api {
  health: Health | Error = okHealth();
  repo: Repository = SAMPLE;
  session: Session = makeSession();
  calls: string[] = [];
  fail: Record<string, ApiError | undefined> = {};
  gates: Record<string, Promise<void> | undefined> = {};
  handlers: EventHandlers | null = null;
  closed = false;

  private async step<T>(name: string, value: () => T): Promise<T> {
    this.calls.push(name);
    if (this.gates[name]) await this.gates[name];
    if (this.fail[name]) throw this.fail[name];
    return structuredClone(value());
  }
  getHealth = () => this.step("getHealth", () => { if (this.health instanceof Error) throw this.health; return this.health; });
  registerSample = () => this.step("registerSample", () => ({ ...this.repo, files: [] }));
  uploadRepository = (f: File) => this.step(`upload:${f.name}`, () => ({ ...this.repo, kind: "upload" as const, name: f.name, files: [] }));
  getRepository = () => this.step("getRepository", () => this.repo);
  createSession = () => this.step("createSession", () => this.session);
  startSession = () => this.step("startSession", () => this.session);
  getSession = () => this.step("getSession", () => this.session);
  approve = (_id: string, sha: string) => this.step(`approve:${sha.slice(0, 4)}`, () => this.session);
  reject = (_id: string, sha: string) => this.step(`reject:${sha.slice(0, 4)}`, () => this.session);
  cancel = () => this.step("cancel", () => this.session);
  openEvents(_id: string, handlers: EventHandlers) {
    this.handlers = handlers;
    this.closed = false;
    return () => { this.closed = true; };
  }

  emitStep(seq: number, state: SessionState, message: string, kind = "state") {
    const event: AgentEvent = { seq, session_id: this.session.session_id, state, kind, message, created_at: "now" };
    act(() => this.handlers?.onStep(event));
  }
  emitSnapshot() { act(() => this.handlers?.onSnapshot(structuredClone(this.session))); }
}

export function gate(): { promise: Promise<void>; open: () => void } {
  let open!: () => void;
  const promise = new Promise<void>((resolve) => { open = resolve; });
  return { promise, open };
}
