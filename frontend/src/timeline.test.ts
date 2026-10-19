import { buildTimeline } from "./timeline";
import type { AgentEvent } from "./types";
import { finished, makeSession, waitingSession } from "./test/fakeApi";

const ev = (seq: number, kind: string, message: string): AgentEvent =>
  ({ seq, session_id: "s", state: "RUN_TESTS", kind, message, created_at: "" });

describe("buildTimeline", () => {
  it("orders rows, drops duplicates and unknown kinds, and marks the last live row as current", () => {
    const rows = buildTimeline([ev(3, "state", "c"), ev(1, "state", "a"), ev(1, "state", "a"), ev(2, "debug", "x")], makeSession());
    expect(rows.map((r) => [r.id, r.status])).toEqual([[1, "done"], [3, "current"]]);
  });

  it("shows warnings and errors with their own status", () => {
    const rows = buildTimeline([ev(1, "info", "Proposed patch could not be safely applied."), ev(2, "error", "boom")], finished("FAILED", "failed"));
    expect(rows.map((r) => r.status)).toEqual(["warn", "failed"]);
  });

  it("does not pulse once the session is finished", () => {
    const rows = buildTimeline([ev(1, "state", "done")], finished("SUCCESS", "success", {}, true));
    expect(rows[0].status).toBe("done");
  });

  it("keeps the final row pulsing while waiting for the human", () => {
    expect(buildTimeline([ev(1, "state", "Waiting")], waitingSession())[0].status).toBe("current");
  });

  it("is empty with no events", () => expect(buildTimeline([], null)).toEqual([]));
});
