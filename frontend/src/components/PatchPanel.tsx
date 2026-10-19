import { diffStats, parseDiff } from "../diff";
import type { PatchView } from "../types";
import { shortHash } from "../uiState";
import type { Pending } from "../useWorkflow";

export function DiffView({ diff }: { diff: string }) {
  const rows = parseDiff(diff);
  return (
    <div className="diff" role="region" aria-label="Proposed code change" tabIndex={0}>
      {rows.map((r, i) => (
        <div key={i} className={`diff-row diff-${r.kind}`}>
          <span className="ln" aria-hidden="true">{r.oldNo ?? ""}</span>
          <span className="ln" aria-hidden="true">{r.newNo ?? ""}</span>
          <span className="sign" aria-hidden="true">{r.kind === "add" ? "+" : r.kind === "del" ? "-" : ""}</span>
          <code className="code">{r.text || " "}</code>
        </div>
      ))}
    </div>
  );
}

interface Props {
  patch: PatchView | { sha256: string; diff: string; summary?: string; files: string[]; attempt?: number };
  awaiting: boolean; pending: Pending; onApprove: () => void; onReject: () => void;
}

export function PatchPanel({ patch, awaiting, pending, onApprove, onReject }: Props) {
  const stats = diffStats(parseDiff(patch.diff));
  const busy = pending === "approve" || pending === "reject";
  return (
    <section className="panel" aria-labelledby="patch-h">
      <h2 id="patch-h">{awaiting ? "Proposed Fix" : "Applied Fix"}</h2>
      <p className="muted">
        {patch.files.join(", ")} · <span className="add-count">+{stats.added}</span> <span className="del-count">-{stats.removed}</span>
        {patch.attempt ? ` · attempt ${patch.attempt}` : ""} · <span title={patch.sha256}>patch {shortHash(patch.sha256)}</span>
      </p>
      {patch.summary && <p>{patch.summary}</p>}
      <DiffView diff={patch.diff} />
      {awaiting && (
        <>
          <p className="caution">
            Review the change before approving. Passing tests show the tests pass, not that the fix is correct.
            Nothing is applied until you approve.
          </p>
          <div className="actions">
            <button type="button" className="danger" disabled={busy} onClick={onReject}>{pending === "reject" ? "Rejecting..." : "Reject"}</button>
            <button type="button" className="primary" disabled={busy} onClick={onApprove}>{pending === "approve" ? "Approving..." : "Approve Fix"}</button>
          </div>
        </>
      )}
    </section>
  );
}
