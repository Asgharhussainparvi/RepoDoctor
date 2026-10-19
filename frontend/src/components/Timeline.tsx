import type { TimelineRow } from "../timeline";

const ICON = { done: "✓", current: "●", failed: "✕", warn: "!" } as const;

export function Timeline({ rows, waiting }: { rows: TimelineRow[]; waiting: boolean }) {
  if (!rows.length) {
    return <p className="muted">{waiting ? <><span className="spinner" aria-hidden="true" /> Starting...</> : "No activity yet. Pick a repository and start debugging."}</p>;
  }
  return (
    <ol className="timeline" aria-label="Debugging progress" aria-live="polite">
      {rows.map((r) => (
        <li key={r.id} className={`step step-${r.status}`}>
          <span className="icon" aria-hidden="true">{r.status === "current" ? <span className="spinner" /> : ICON[r.status]}</span>
          <span>{r.message}</span>
        </li>
      ))}
    </ol>
  );
}
