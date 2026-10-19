// Mirrors the Python API (repodoctor/api/schemas.py and repodoctor/agent/schemas.py).

export type SessionState =
  | "INITIALIZE" | "ANALYZE_REPOSITORY" | "RUN_TESTS" | "ANALYZE_FAILURE" | "INVESTIGATE" | "FORM_HYPOTHESIS"
  | "GENERATE_PATCH" | "WAITING_APPROVAL" | "APPLY_PATCH" | "VERIFY" | "RETRY"
  | "SUCCESS" | "FAILED" | "REJECTED" | "CANCELLED" | "NO_FAILURE";

export const TERMINAL: ReadonlySet<SessionState> = new Set<SessionState>([
  "SUCCESS", "FAILED", "REJECTED", "CANCELLED", "NO_FAILURE",
]);

// The 13 explicit UI states from the product spec: `idle` and `uploading` are client-side, the rest come from the server.
export type UiState =
  | "idle" | "uploading" | "analyzing" | "running tests" | "investigating" | "patch proposed"
  | "awaiting approval" | "applying patch" | "verifying" | "success" | "failed" | "timeout" | "cancelled";

export interface Counts {
  passed: number; failed: number; errors: number; skipped: number;
  xfailed: number; xpassed: number; deselected: number; warnings: number;
}

export interface Verification {
  verified: boolean; reason_code: string; message: string; before: Counts; after: Counts;
  previously_failing: string[]; still_failing: string[]; new_failures: string[];
  files_changed: string[]; patch_sha256: string;
}

export interface AttemptFeedback { attempt: number; patch_sha256: string | null; reason_code: string; message: string }

export interface Report {
  session_id: string; outcome: SessionState; headline: string; message: string;
  root_cause: { file: string; line: number | null; description: string } | null;
  before: Counts | null; after: Counts | null;
  failing_tests_before: string[]; failing_tests_after: string[];
  files_changed: string[]; patch_diff: string | null; patch_sha256: string | null;
  verification: Verification | null; patch_attempts: number; attempt_feedback: AttemptFeedback[];
}

export interface PatchView { sha256: string; diff: string; summary: string; files: string[]; attempt: number; status: string }

export interface EngineInfo { kind: "bedrock" | "demo" | "custom"; model_id: string | null; simulated: boolean }

export interface Session {
  session_id: string; state: SessionState; ui_state: UiState; message: string; error_code: string | null;
  patch_attempts: number; pending_patch: PatchView | null; report: Report | null;
  repository_id: string | null; engine: EngineInfo;
}

export interface AgentEvent { seq: number; session_id: string; state: SessionState; kind: string; message: string; created_at: string }

export interface Repository {
  id: string; name: string; kind: "upload" | "sample"; file_count: number; total_bytes: number;
  created_at: string; files: string[];
}

export interface ComponentStatus { ok: boolean; message: string }
export interface Health {
  status: "ok" | "degraded"; version: string; sandbox: ComponentStatus; ai: ComponentStatus;
  engine: EngineInfo; active_sessions: number; max_active_sessions: number;
}
