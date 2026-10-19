import type { Counts, Session, UiState } from "./types";
import { TERMINAL } from "./types";

export const STATE_LABEL: Record<UiState, string> = {
  idle: "Idle",
  uploading: "Uploading repository",
  analyzing: "Analyzing repository",
  "running tests": "Running tests",
  investigating: "Investigating",
  "patch proposed": "Patch proposed",
  "awaiting approval": "Awaiting your approval",
  "applying patch": "Applying patch",
  verifying: "Verifying",
  success: "Success",
  failed: "Failed",
  timeout: "Timed out",
  cancelled: "Cancelled",
};

export const isActive = (s: Session | null): boolean => !!s && !TERMINAL.has(s.state);

/** The ONLY place that decides whether the UI may say a bug was fixed. */
export function isVerifiedSuccess(s: Session | null): boolean {
  const v = s?.report?.verification;
  return !!s && s.state === "SUCCESS" && !!v && v.verified === true && v.after.failed === 0 && v.after.errors === 0;
}

export type Tone = "success" | "neutral" | "error" | "warning";
export interface Banner { tone: Tone; title: string; detail: string }

export function outcomeBanner(s: Session | null): Banner | null {
  if (!s || !TERMINAL.has(s.state)) return null;
  switch (s.state) {
    case "SUCCESS":
      return isVerifiedSuccess(s)
        ? { tone: "success", title: "DEBUGGING COMPLETE", detail: "Verification: CONFIRMED. The tests were re-run after the patch and all pass." }
        : { tone: "error", title: "Result could not be verified", detail: "RepoDoctor will not claim success without a confirmed verification." };
    case "NO_FAILURE":
      return { tone: "neutral", title: "Nothing to fix", detail: s.message };
    case "FAILED":
      return { tone: s.ui_state === "timeout" ? "warning" : "error", title: s.message || "The session failed.", detail: s.error_code ? `Code: ${s.error_code}` : "" };
    default:
      return { tone: "neutral", title: s.state === "REJECTED" ? "Patch rejected" : "Session cancelled", detail: "No changes were applied." };
  }
}

export const formatCounts = (c: Counts | null | undefined): string =>
  c ? `${c.failed} failed / ${c.passed} passed${c.errors ? ` / ${c.errors} errors` : ""}` : "n/a";

export function shortHash(sha: string): string { return sha.slice(0, 10); }
