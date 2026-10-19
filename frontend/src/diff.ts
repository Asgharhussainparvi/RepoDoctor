export type DiffKind = "meta" | "hunk" | "add" | "del" | "ctx" | "note";
export interface DiffRow { kind: DiffKind; oldNo: number | null; newNo: number | null; text: string }

/** Parse a unified diff into display rows with old/new line numbers. Pure; never throws. */
export function parseDiff(diff: string): DiffRow[] {
  const lines = diff.replace(/\r\n/g, "\n").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  const rows: DiffRow[] = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const startsFile = line.startsWith("diff --git ") || (line.startsWith("--- ") && (lines[i + 1] ?? "").startsWith("+++ "));
    if (startsFile) inHunk = false;
    if (line.startsWith("@@")) {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      oldNo = m ? Number(m[1]) : 0;
      newNo = m ? Number(m[2]) : 0;
      inHunk = true;
      rows.push({ kind: "hunk", oldNo: null, newNo: null, text: line });
    } else if (!inHunk) {
      rows.push({ kind: "meta", oldNo: null, newNo: null, text: line });
    } else if (line.startsWith("+")) {
      rows.push({ kind: "add", oldNo: null, newNo: newNo++, text: line.slice(1) });
    } else if (line.startsWith("-")) {
      rows.push({ kind: "del", oldNo: oldNo++, newNo: null, text: line.slice(1) });
    } else if (line.startsWith("\\")) {
      rows.push({ kind: "note", oldNo: null, newNo: null, text: line.slice(1).trim() });
    } else {
      rows.push({ kind: "ctx", oldNo: oldNo++, newNo: newNo++, text: line.startsWith(" ") ? line.slice(1) : line });
    }
  }
  return rows;
}

export function diffStats(rows: DiffRow[]): { added: number; removed: number } {
  return {
    added: rows.filter((r) => r.kind === "add").length,
    removed: rows.filter((r) => r.kind === "del").length,
  };
}
