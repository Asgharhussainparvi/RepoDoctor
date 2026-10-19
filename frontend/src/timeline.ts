import type { AgentEvent, Session, SessionState } from "./types";
import { TERMINAL } from "./types";

export type RowStatus = "done" | "current" | "failed" | "warn";
export interface TimelineRow { id: number; message: string; status: RowStatus; state: SessionState }

/** Turn the server's high-level events into rows. Never shows model reasoning, only progress. */
export function buildTimeline(events: AgentEvent[], session: Session | null): TimelineRow[] {
  const seen = new Set<number>();
  const rows: TimelineRow[] = [];
  for (const e of events) {
    if (seen.has(e.seq) || !["state", "info", "error"].includes(e.kind)) continue;
    seen.add(e.seq);
    rows.push({
      id: e.seq, message: e.message, state: e.state,
      status: e.kind === "error" ? "failed" : e.kind === "info" ? "warn" : "done",
    });
  }
  rows.sort((a, b) => a.id - b.id);
  if (session && !TERMINAL.has(session.state)) {
    const last = rows[rows.length - 1];
    if (last && last.status === "done") last.status = "current";
  }
  return rows;
}
