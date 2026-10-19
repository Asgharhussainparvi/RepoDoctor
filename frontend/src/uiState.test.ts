import { formatCounts, isVerifiedSuccess, outcomeBanner } from "./uiState";
import { counts, finished, makeSession, waitingSession } from "./test/fakeApi";

describe("success is only claimed when verification is confirmed", () => {
  it("accepts a real verified success", () => {
    const s = finished("SUCCESS", "success", {}, true);
    expect(isVerifiedSuccess(s)).toBe(true);
    expect(outcomeBanner(s)).toMatchObject({ tone: "success", title: "DEBUGGING COMPLETE" });
  });

  it("refuses SUCCESS without a verification, or with an unverified one", () => {
    const unverified = finished("SUCCESS", "success", {}, false);
    const missing = finished("SUCCESS", "success", {});
    missing.report!.verification = null;
    for (const s of [unverified, missing, makeSession({ state: "SUCCESS", ui_state: "success" })]) {
      expect(isVerifiedSuccess(s)).toBe(false);
      expect(outcomeBanner(s)).toMatchObject({ tone: "error", title: "Result could not be verified" });
    }
  });

  it("refuses a verified flag that still has failing tests", () => {
    const s = finished("SUCCESS", "success", {}, true);
    s.report!.verification!.after = counts(31, 1);
    expect(isVerifiedSuccess(s)).toBe(false);
  });

  it("never celebrates other outcomes", () => {
    for (const s of [finished("FAILED", "failed", { message: "I could not safely verify a fix." }), finished("REJECTED", "cancelled"),
                     finished("CANCELLED", "cancelled"), finished("NO_FAILURE", "success", { message: "All tests pass." })]) {
      expect(outcomeBanner(s)?.tone).not.toBe("success");
      expect(isVerifiedSuccess(s)).toBe(false);
    }
  });
});

describe("other banners", () => {
  it("has none while running or waiting", () => {
    expect(outcomeBanner(waitingSession())).toBeNull();
    expect(outcomeBanner(null)).toBeNull();
  });
  it("uses a warning tone for timeouts and an error tone for other failures", () => {
    expect(outcomeBanner(finished("FAILED", "timeout", { message: "Test execution exceeded the allowed time.", error_code: "test_timeout" }))?.tone).toBe("warning");
    expect(outcomeBanner(finished("FAILED", "failed", { message: "x", error_code: "ai_unavailable" }))).toMatchObject({ tone: "error", detail: "Code: ai_unavailable" });
  });
  it("states that nothing was applied for rejection and cancellation", () => {
    expect(outcomeBanner(finished("REJECTED", "cancelled"))).toMatchObject({ title: "Patch rejected", detail: "No changes were applied." });
    expect(outcomeBanner(finished("CANCELLED", "cancelled"))?.title).toBe("Session cancelled");
  });
  it("formats counts", () => {
    expect(formatCounts(counts(31, 1))).toBe("1 failed / 31 passed");
    expect(formatCounts(null)).toBe("n/a");
  });
});
