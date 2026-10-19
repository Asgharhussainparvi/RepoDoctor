import type { Report } from "../types";
import { formatCounts } from "../uiState";

export function VerificationPanel({ report, confirmed }: { report: Report; confirmed: boolean }) {
  const v = report.verification;
  return (
    <section className="panel" aria-labelledby="verify-h">
      <h2 id="verify-h">Verification</h2>
      <dl className="facts">
        {report.root_cause && (
          <>
            <dt>Root cause</dt>
            <dd>{report.root_cause.file}{report.root_cause.line ? `:${report.root_cause.line}` : ""}. {report.root_cause.description}</dd>
          </>
        )}
        <dt>Before</dt>
        <dd data-testid="before">{formatCounts(report.before)}</dd>
        <dt>After</dt>
        <dd data-testid="after">{report.after ? <>{formatCounts(report.after)}{confirmed ? " ✓" : ""}</> : "not run"}</dd>
        {report.files_changed.length > 0 && (<><dt>Files changed</dt><dd>{report.files_changed.length} ({report.files_changed.join(", ")})</dd></>)}
        <dt>Verification</dt>
        <dd data-testid="verdict">{confirmed ? "CONFIRMED ✓" : v ? `NOT CONFIRMED (${v.reason_code})` : "not performed"}</dd>
        <dt>Patch attempts</dt>
        <dd>{report.patch_attempts}</dd>
      </dl>
      {report.failing_tests_before.length > 0 && (
        <details>
          <summary>Failing tests before ({report.failing_tests_before.length})</summary>
          <ul>{report.failing_tests_before.map((t) => <li key={t}><code>{t}</code></li>)}</ul>
        </details>
      )}
      {report.attempt_feedback.length > 0 && (
        <details open={!confirmed}>
          <summary>What did not work ({report.attempt_feedback.length})</summary>
          <ul>{report.attempt_feedback.map((f, i) => <li key={i}>Attempt {f.attempt}: <code>{f.reason_code}</code>. {f.message}</li>)}</ul>
        </details>
      )}
    </section>
  );
}
